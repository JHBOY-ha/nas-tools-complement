"""Offline operation-count and safety regressions; no NAS or external services."""
import collections
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import event

from tests.test_subtitle_tasks import _MemoryDb, _File, SRT
from app.db.models import SUBTITLEAUDITSTATE, SUBTITLEMEDIASTATUS, SUBTITLEPROBECACHE, SUBTITLETASK, SUBTITLETASKITEM
from app.helper.subtitle_tasks import DEFAULT_POLICY, SubtitleTaskManager, TaskStorageInsufficient
from app.helper.subtitle_media_status import SubtitleMediaStatusStore
from app.helper.subtitle_health import SubtitleHealth
from app.helper.subtitle_align import SubtitleAligner
from app.subtitle import Subtitle


class SubtitleDatabasePerformanceTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = _MemoryDb()
        self.db.init_db()
        self.addCleanup(self.db.engine.dispose)
        self.addCleanup(self.db._session.remove)
        self.manager = SubtitleTaskManager(db=self.db, staging_root=os.path.join(self.temp.name, 'staging'))
        os.makedirs(self.manager._staging_root)
        self.sql = []
        event.listen(self.db.engine, 'before_cursor_execute', self.record_sql)
        self.addCleanup(event.remove, self.db.engine, 'before_cursor_execute', self.record_sql)

    def record_sql(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append((statement, parameters, executemany))

    def counts(self):
        return collections.Counter(statement.split()[0].upper() for statement, _, _ in self.sql)

    def task_rows(self, count, status='succeeded', task_type='upload', **overrides):
        now = time.time()
        rows = [dict(ID=str(i), TYPE=task_type, OWNER='review', STATUS=status,
                     CREATED_AT=now, UPDATED_AT=now, FINISHED_AT=now, **overrides) for i in range(count)]
        self.db.session.execute(SUBTITLETASK.__table__.insert(), rows)
        self.db.commit()

    def test_bulk_audit_and_snapshot_writes_preserve_values_and_immutable_versions(self):
        states = {f'/review/{i}.mkv': {'status': 'ok'} for i in range(1000)}
        self.sql.clear()
        self.manager.upsert_audit_states('scope', 'emby', states)
        # Two executemany batches plus ONE publication marker, not one INSERT
        # or SELECT per media. Constant receipt/clock reads enforce atomicity.
        self.assertEqual(self.counts()['INSERT'], 3)
        self.assertEqual(self.counts()['SELECT'], 3)
        loaded = self.db.query(SUBTITLEAUDITSTATE).filter_by(SUBTITLE_PATH='/review/0.mkv').one()
        states['/review/0.mkv']['status'] = 'warning'
        self.sql.clear()
        self.manager.upsert_audit_states('scope', 'emby', states)
        self.assertEqual(self.counts()['INSERT'], 3)
        self.assertEqual(loaded.STATUS, 'ok')
        current = self.db.query(SUBTITLEAUDITSTATE).filter_by(SUBTITLE_PATH='/review/0.mkv').one()
        self.assertNotEqual(loaded.ID, current.ID)
        self.assertEqual(current.STATUS, 'warning')
        self.assertEqual(json.loads(current.RESULT)['checked_at'],
                         self.manager.get_audit_states('scope', 'emby')['/review/0.mkv']['checked_at'])
        self.sql.clear()
        store = SubtitleMediaStatusStore(db=self.db)
        store.upsert_many('emby', [dict(media_path=path, has_external=False) for path in states])
        self.assertEqual(self.counts()['SELECT'], 3)
        self.assertEqual(self.counts()['INSERT'], 3)
        self.assertFalse(store.get('emby', '/review/0.mkv')['has_external'])
        # A duplicate key in one batch must update, not fail a unique constraint.
        store.upsert_many('emby', [dict(media_path='/review/0.mkv', has_external=value)
                                  for value in (False, True)])
        self.assertTrue(store.get('emby', '/review/0.mkv')['has_external'])

    def test_bulk_audit_failure_rolls_back_both_state_tables_and_terminal_status(self):
        self.task_rows(1, status='running', task_type='audit')
        states = {f'/review/{i}.mkv': {'status': 'ok'} for i in range(501)}
        calls = [0]

        def fail_second_batch(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO "SUBTITLE_AUDIT_STATE"'):
                calls[0] += 1
                if calls[0] == 2:
                    raise RuntimeError('second batch failure')

        event.listen(self.db.engine, 'before_cursor_execute', fail_second_batch)
        try:
            with self.assertRaisesRegex(RuntimeError, 'second batch'):
                self.manager.commit_audit_result('0', 'scope', 'emby', states, {}, 'succeeded', 'done',
                                                 media_snapshots=[{'media_path': '/review/0.mkv'}])
        finally:
            event.remove(self.db.engine, 'before_cursor_execute', fail_second_batch)
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).count(), 0)
        self.assertEqual(self.db.query(SUBTITLEMEDIASTATUS).count(), 0)
        self.assertEqual(self.db.query(SUBTITLETASK).one().STATUS, 'running')

    def test_retained_cleanup_and_admission_do_not_query_or_probe_each_cleaned_task(self):
        self.task_rows(2000)
        policy = dict(DEFAULT_POLICY, task_retention_count=2000)
        self.manager._last_persistent_cache_cleanup = time.time()
        with patch.object(self.manager, 'get_settings', return_value=policy), \
                patch('app.helper.subtitle_tasks.Config') as config:
            config.return_value.get_config_path.return_value = self.temp.name
            self.sql.clear()
            self.manager.cleanup()
            self.assertLessEqual(self.counts()['SELECT'], 10)
            self.sql.clear()
            with patch.object(self.manager, '_remove_tree', side_effect=AssertionError('repeat cleanup')), \
                    patch.object(self.manager, '_terminal_staging_present', side_effect=AssertionError('repeat probe')):
                self.manager.cleanup()
                self.assertEqual(self.manager._active_staging_reservation_totals(), (0, 0))
            self.assertLessEqual(self.counts()['SELECT'], 4)

    def test_cleanup_is_throttled_but_explicit_calls_and_failed_passes_can_retry(self):
        with patch.object(self.manager, '_cleanup_retained_tasks') as cleanup:
            self.manager.cleanup(force=False)
            self.manager.cleanup(force=False)
            self.assertEqual(cleanup.call_count, 1)
            self.manager.cleanup()
            self.assertEqual(cleanup.call_count, 2)
        self.manager._last_cleanup = None
        with patch.object(self.manager, '_cleanup_retained_tasks', side_effect=[RuntimeError('fail'), None]) as cleanup, \
                patch('app.helper.subtitle_tasks.time.monotonic', return_value=0) as clock:
            with self.assertRaises(RuntimeError):
                self.manager.cleanup(force=False)
            clock.return_value = 30
            self.manager.cleanup(force=False)
            self.assertEqual(cleanup.call_count, 1)
            clock.return_value = 60
            self.manager.cleanup(force=False)
            self.assertEqual(cleanup.call_count, 2)

    def test_missing_directory_does_not_bypass_unprocessed_ownership_markers(self):
        self.task_rows(1)
        self.assertFalse(self.manager._terminal_staging_present('0', {}))
        with patch.object(self.manager, '_cleanup_upload_temp_artifacts', return_value=set()) as markers:
            self.assertTrue(self.manager._cleanup_task_staging('0'))
        markers.assert_called_once()
        self.assertTrue(self.manager._staging_known_absent('0', cleaned=True))
        # Negative evidence is bounded in time and cannot hide a recreated tree forever.
        self.manager._staging_absent_cache['0'] = (0, True)
        os.makedirs(os.path.join(self.manager._staging_root, '0'))
        self.assertTrue(self.manager._terminal_staging_present('0', {}))

    def test_target_reservations_use_two_queries_and_reflect_item_completion(self):
        payload = json.dumps({'target_volume_key': 'volume'})
        self.task_rows(20, status='running', PAYLOAD=payload, POLICY='{}')
        self.db.session.execute(SUBTITLETASKITEM.__table__.insert(), [dict(
            TASK_ID=str(i), ITEM_KEY=str(i), LOGICAL_INDEX=0, KIND='text', SOURCE_NAME='s.srt',
            STAGED_PATH='/review/s.srt', CONTENT_HASH='hash', CREATED_AT=time.time(),
            SIZE=10, COMPANION_SIZE=5, STATUS='pending', UPDATED_AT=time.time()
        ) for i in range(20)])
        self.db.commit()
        self.sql.clear()
        total, _ = self.manager._active_target_reservations('volume')
        self.assertEqual(total, 600)
        self.assertEqual(self.counts()['SELECT'], 2)
        self.db.query(SUBTITLETASKITEM).filter_by(TASK_ID='0').update({'STATUS': 'succeeded'})
        self.db.commit()
        self.assertEqual(self.manager._active_target_reservations('volume')[0], 570)
        self.assertEqual(self.manager._active_target_reservations('other')[0], 0)

    def test_cache_overflow_exceeds_sqlite_parameter_limit_without_failure(self):
        now = time.time()
        # This previously built 32767 bind parameters on a 32766-limit SQLite.
        for start in range(0, 82767, 1000):
            self.db.session.execute(SUBTITLEPROBECACHE.__table__.insert(), [dict(
                SERVER='emby', PATH=f'/review/{i}', FINGERPRINT='{}', CREATED_AT=now, UPDATED_AT=now
            ) for i in range(start, min(start + 1000, 82767))])
        self.db.commit()
        with patch('app.helper.subtitle_tasks.Config') as config:
            config.return_value.get_config_path.return_value = self.temp.name
            self.manager.cleanup()
        self.assertEqual(self.db.query(SUBTITLEPROBECACHE).count(), 50000)
        self.assertGreater(self.manager._last_persistent_cache_cleanup, 0)
        self.assertIsNotNone(self.db.query(SUBTITLEPROBECACHE).filter_by(PATH='/review/82766').first())

    def test_probe_cache_batches_and_invalidation_remain_bounded(self):
        entries = [(dict(path=f'/review/{i}', size=1), {'valid': True}) for i in range(1001)]
        self.manager.probe_cache_put_many('emby', entries)
        self.assertEqual(self.counts()['INSERT'], 3)
        self.sql.clear()
        self.assertEqual(self.manager.invalidate_probe_cache([fp['path'] for fp, _ in entries]), 1001)
        self.assertEqual(self.counts()['DELETE'], 3)
        for statement, parameters, many in self.sql:
            self.assertLessEqual(len(parameters), 999)

    def test_settings_cache_copies_results_and_invalidates_on_save(self):
        self.sql.clear()
        original = self.manager.get_settings()
        original['max_upload_queue'] = 32
        self.assertEqual(self.manager.get_settings()['max_upload_queue'], DEFAULT_POLICY['max_upload_queue'])
        self.assertEqual(self.counts()['SELECT'], 1)
        with patch.object(self.manager, '_validate_disk_policy'):
            self.manager.update_settings({'max_upload_queue': 3})
        self.assertEqual(self.manager.get_settings()['max_upload_queue'], 3)

    def test_queue_positions_keep_priority_and_tiebreak_order(self):
        self.task_rows(4, status='queued', PRIORITY=0)
        self.db.query(SUBTITLETASK).filter_by(ID='2').update({'PRIORITY': 1})
        self.db.commit()
        for expected, task_id in enumerate(['2', '0', '1', '3'], 1):
            row = self.db.query(SUBTITLETASK).filter_by(ID=task_id).one()
            self.sql.clear()
            self.assertEqual(self.manager._queue_position(row), expected)
            self.assertEqual(self.counts()['SELECT'], 1)
            self.assertIn('count(', self.sql[0][0].lower())

    def test_spooling_samples_space_but_debits_each_write_and_rechecks_at_end(self):
        policy = dict(DEFAULT_POLICY)
        reserve = policy['reserve_free_mb'] * 1024 * 1024
        raw_dir = os.path.join(self.temp.name, 'raw'); os.makedirs(raw_dir)
        file = _File('sample.sub', b'x' * (8 * 1024 * 1024))
        with patch('app.helper.subtitle_tasks.time.monotonic', return_value=0), \
                patch('app.helper.subtitle_tasks.isolated_disk_usage', return_value=SimpleNamespace(free=reserve + 100*1024*1024)) as usage:
            self.manager._spool_files([file], raw_dir, policy)
            self.assertEqual(usage.call_count, 2)
        # A constant sampled value must not let successive writes spend the same free bytes.
        with patch('app.helper.subtitle_tasks.time.monotonic', return_value=0), \
                patch('app.helper.subtitle_tasks.isolated_disk_usage', return_value=SimpleNamespace(free=reserve + 10*1024*1024)) as usage:
            with self.assertRaises(TaskStorageInsufficient):
                self.manager._spool_files([file], raw_dir, policy)
            self.assertEqual(usage.call_count, 1)
        with patch('app.helper.subtitle_tasks.isolated_disk_usage', side_effect=[SimpleNamespace(free=10**12), SimpleNamespace(free=0)]):
            with self.assertRaises(TaskStorageInsufficient):
                self.manager._spool_files([_File('sample.srt', SRT)], raw_dir, policy)


class SubtitleFilePerformanceTest(TestCase):
    def test_opensubtitles_preflight_runs_outside_api_lock_and_rechecks_races(self):
        service = Subtitle()
        held = [False]
        calls = []

        class Lock:
            def __enter__(self):
                held[0] = True

            def __exit__(self, *args):
                held[0] = False

        with tempfile.TemporaryDirectory() as root:
            item = {'file': os.path.join(root, 'Movie'), 'name': 'Movie'}

            def existing(_item, **kwargs):
                calls.append(held[0])
                return os.path.join(root, 'Movie.chi.srt')

            with patch.object(service, 'opensubtitles', SimpleNamespace(languages=['zh-cn'])), \
                    patch.object(service, '_opensubtitles_lock', Lock()), \
                    patch.object(service.__class__, '_Subtitle__existing_opensubtitles_target', side_effect=existing):
                self.assertTrue(service._Subtitle__download_opensubtitles_item(item)[0])
                self.assertEqual(calls, [False])

            calls.clear()

            identity_calls = []
            def changed_identity(_path):
                identity_calls.append(_path)
                # Claims now precede preflight. Inject the publication race
                # after preflight rather than before its snapshot is captured.
                if len(identity_calls) == 2:
                    service._opensubtitles_publication_generation += 1
                return (1, 2, 3, 4, 5)

            with patch.object(service, 'opensubtitles', SimpleNamespace(languages=['zh-cn'])), \
                    patch.object(service, '_opensubtitles_lock', Lock()), \
                    patch.object(service.__class__, '_Subtitle__opensubtitles_path_identity', side_effect=changed_identity), \
                    patch.object(service.__class__, '_Subtitle__existing_opensubtitles_target', side_effect=existing):
                self.assertTrue(service._Subtitle__download_opensubtitles_item(item)[0])
                self.assertEqual(calls, [False, False], 'Race rechecks must also keep NAS I/O outside the shared lock')

    def test_repair_hash_guard_detects_in_place_changes_with_preserved_mtime(self):
        # Digest propagation must not replace the checks before old-source deletion.
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'old.srt')
            source.write_bytes(b'original')
            service = Subtitle()
            before = source.stat()
            snapshot = service._Subtitle__file_snapshot(str(source), hashlib.sha256(b'original').hexdigest())
            source.write_bytes(b'modified')
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertFalse(service._Subtitle__file_snapshot_matches(str(source), snapshot, verify_hash=True))

    def test_opensubtitles_rechecks_language_when_bare_subtitle_changes_while_waiting(self):
        service = Subtitle()
        with tempfile.TemporaryDirectory() as root:
            subtitle = Path(root, 'Movie.srt')
            subtitle.write_text('This is an English subtitle with enough letters.')
            item = {'file': os.path.join(root, 'Movie'), 'name': 'Movie'}
            directory_before = service._Subtitle__opensubtitles_path_identity(root)

            class WriterLock:
                def __enter__(self):
                    # In-place edits do not change the parent directory stamp.
                    subtitle.write_text('这是修改后的中文字幕内容')

                def __exit__(self, *args):
                    return False

            with patch.object(service, 'opensubtitles', SimpleNamespace(languages=['zh-cn'])), \
                    patch.object(service, '_opensubtitles_lock', WriterLock()):
                success, message = service._Subtitle__download_opensubtitles_item(item)
            self.assertEqual(directory_before, service._Subtitle__opensubtitles_path_identity(root))
            self.assertTrue(success)
            self.assertIn('字幕已存在', message)

    def test_translation_cache_has_lru_bound_and_expires_unvisited_keys(self):
        cls = SubtitleAligner
        with patch.object(cls, '_llm_translation_cache', {}), patch.object(cls, '_llm_translation_cache_limit', 2):
            cls._SubtitleAligner__set_translation_cache('a', {0: 'a'})
            cls._SubtitleAligner__set_translation_cache('b', {0: 'b'})
            cls._SubtitleAligner__get_translation_cache('a')[0] = 'modified'
            cls._SubtitleAligner__set_translation_cache('c', {0: 'c'})
            self.assertIsNone(cls._SubtitleAligner__get_translation_cache('b'))
            self.assertEqual(cls._SubtitleAligner__get_translation_cache('a'), {0: 'a'})
            cls._llm_translation_cache['a'] = (0, {0: 'a'})
            cls._SubtitleAligner__set_translation_cache('d', {0: 'd'})
            self.assertNotIn('a', cls._llm_translation_cache)

    def test_reference_cleanup_throttles_reads_and_enforces_write_capacity(self):
        cls = SubtitleAligner
        with tempfile.TemporaryDirectory() as root, patch.object(cls, '_reference_cache_maintenance', {}):
            def write(name):
                Path(root, name+'.srt').write_bytes(b'x'*8)
                Path(root, name+'.json').write_text('{}')
            write('a')
            real = cls._SubtitleAligner__prune_reference_cache
            with patch.object(cls, '_SubtitleAligner__prune_reference_cache', wraps=real) as prune:
                cls._SubtitleAligner__maintain_reference_cache(root, 10)
                cls._SubtitleAligner__maintain_reference_cache(root, 10)
                self.assertEqual(prune.call_count, 1)
                write('b')
                cls._SubtitleAligner__maintain_reference_cache(root, 10, added_bytes=8)
                self.assertEqual(prune.call_count, 2)
                self.assertLessEqual(sum(p.stat().st_size for p in Path(root).glob('*.srt')), 10)

    def test_reference_eviction_failure_keeps_bytes_accounted_and_retries(self):
        cls = SubtitleAligner
        with tempfile.TemporaryDirectory() as root, patch.object(cls, '_reference_cache_maintenance', {}):
            Path(root, 'large.srt').write_bytes(b'x' * 20)
            Path(root, 'large.json').write_text('{}')
            with patch('app.helper.subtitle_align.os.remove', side_effect=PermissionError('NAS busy')):
                cls._SubtitleAligner__maintain_reference_cache(root, 10)
                self.assertGreater(cls._reference_cache_maintenance[root][2], 10)
            cls._SubtitleAligner__maintain_reference_cache(root, 10)
            self.assertFalse(Path(root, 'large.srt').exists())

    def test_ffprobe_lookup_is_shared_and_path_changes_are_immediate(self):
        cls = SubtitleHealth
        with patch.object(cls, '_ffprobe_lookup', None), patch('app.helper.subtitle_health.shutil.which', return_value='ffprobe') as which:
            cls._SubtitleHealth__ffprobe_executable()
            cls._SubtitleHealth__ffprobe_executable()
            self.assertEqual(which.call_count, 1)
            with patch.dict(os.environ, {'PATH': '/review/tools'}):
                cls._SubtitleHealth__ffprobe_executable()
            self.assertEqual(which.call_count, 2)

    def test_normalized_and_aligned_hashes_match_written_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'subtitle.srt')
            path.write_bytes(SRT.replace(b'\n', b'\r\n'))
            result = SubtitleHealth.normalize_uploaded_subtitle(str(path))
            self.assertTrue(result['valid'])
            self.assertTrue(result['normalized'])
            self.assertEqual(result['content_hash'], hashlib.sha256(path.read_bytes()).hexdigest())
            digest = SubtitleAligner.write_file(str(path), SubtitleAligner.parse_file(str(path)))
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_upload_reuses_normalization_and_skipped_alignment_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            staged = Path(root, 'upload.srt'); staged.write_bytes(SRT.replace(b'\n', b'\r\n'))
            media = Path(root, 'Movie.mkv'); media.touch()
            service = Subtitle()
            real_hash = service._Subtitle__file_sha256
            paths = []
            def record(path, cancel_check=None):
                paths.append(path)
                return real_hash(path, cancel_check=cancel_check)
            with patch.object(service.__class__, '_Subtitle__file_sha256', side_effect=record), \
                    patch.object(SubtitleAligner, 'align_subtitle', return_value={'applied': False, 'skipped': True}):
                result = service.process_staged_upload(str(staged), 'upload.eng.srt', str(media),
                    work_dir=os.path.join(root, 'work'), align_mode='auto',
                    trusted_source_hash=hashlib.sha256(staged.read_bytes()).hexdigest())
            self.assertEqual(result['output_hash'], hashlib.sha256(SRT).hexdigest())
            self.assertFalse(any('/normalized/' in p or '/aligned/' in p for p in paths), paths)

    def test_deep_audit_skips_links_and_avoids_per_media_islink_calls(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(100):
                Path(root, f'Movie{index}.mkv').touch()
            os.symlink(Path(root, 'Movie0.mkv'), Path(root, 'Linked.mkv'))
            os.symlink(root, Path(root, 'loop'))
            real = os.path.islink
            with patch('app.helper.subtitle_health.os.path.islink', wraps=real) as islink:
                result = SubtitleHealth.audit_roots([root], 'emby')
            self.assertLessEqual(islink.call_count, 2)
            self.assertEqual(len(result['media_snapshots']), 100)
            self.assertNotIn('Linked.mkv', str(result['media_snapshots']))

    def test_partial_deep_enumeration_never_publishes_negative_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'Movie.mkv').touch()

            class BrokenScan:
                def __enter__(self):
                    def entries():
                        yield SimpleNamespace(name='Movie.mkv', is_symlink=lambda: False,
                                              is_dir=lambda **kwargs: False)
                        raise OSError('NAS disconnected during enumeration')
                    return entries()

                def __exit__(self, *args):
                    return False

            with patch('app.helper.subtitle_health.os.scandir', return_value=BrokenScan()):
                result = SubtitleHealth.audit_roots([root], 'emby')
            self.assertEqual(result['media_snapshots'], [])
            self.assertTrue(result['partial'])
