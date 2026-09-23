import ast
import hashlib
import io
import json
import os
import stat
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from werkzeug.datastructures import FileStorage, MultiDict
from tests.test_subtitle_task_security import _UploadManager, _path_snapshot, processors
from app.helper.subtitle_season_pack import build_plan, episode_key, open_pack, open_season_source

SRT = b'1\n00:00:01,000 --> 00:00:02,000\nThis is the first English subtitle sentence.\n'
POLICY = dict(batch_limit_mb=20, text_file_limit_mb=1, season_max_batch_items=100,
              llm_max_batch_items=5)


def pack(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    data.seek(0)
    return data


def rar_pack(payload=SRT, name=b'S01E01.srt'):
    """Build a minimal RAR 4.x archive holding one stored member."""
    import struct
    import zlib

    def header(kind, flags, body=b''):
        raw = struct.pack('<BHH', kind, flags, 7 + len(body)) + body
        return struct.pack('<H', zlib.crc32(raw) & 65535) + raw

    body = struct.pack('<IIBIIBBHI', len(payload), len(payload), 3,
                       zlib.crc32(payload), 0, 20, 0x30, len(name), 0o100644) + name
    return (b'Rar!\x1a\x07\x00' + header(0x73, 0, b'\0' * 6)
            + header(0x74, 0x8000, body) + payload + header(0x7b, 0))


class SeasonMatchingTest(unittest.TestCase):
    def test_common_season_and_episode_names(self):
        for name in ['Show.S02E03.zh.srt', 'Season 2/EP03.ass', '第二季/第三集.srt',
                     '[Show][03][1080p].ass', 'Show - 03.chs.srt', '03.zh.srt', 'Show.2x03.srt']:
            with self.subTest(name=name):
                self.assertEqual(episode_key(name, 2), ((2, 3), ''))

    def test_never_guess_ranges_conflicting_seasons_or_release_numbers(self):
        for name in ['Show.S01E03.srt', 'Season 2/Show.S01E03.srt', 'Show.S02E01-E03.srt',
                     'Show.S02E01E02.srt', 'Show 2016 1080p.srt', 'Show.[01][02].ass',
                     '第1-3集.srt', 'Show.2x01-03.srt', 'Show.S02.srt']:
            with self.subTest(name=name):
                self.assertIsNone(episode_key(name, 2)[0])

    def test_duplicate_video_versions_unmatched_and_plan_stable_for_status_changes(self):
        episodes = [dict(path='/show/a.mkv', season=1, episode=1),
                    dict(path='/show/a2.mkv', season=1, episode=1),
                    dict(path='/show/b.mkv', season=1, episode=2)]
        with open_pack(pack([('E01.srt', SRT), ('E02.srt', SRT), ('E03.srt', SRT)]), POLICY) as (_, members):
            plan = build_plan(members, episodes, 1, 'hash')
            self.assertIn('多个视频', plan['rows'][0]['reason'])
            self.assertEqual(plan['rows'][1]['target']['path'], '/show/b.mkv')
            self.assertIsNone(plan['rows'][2]['target'])
            episodes[2]['subtitle_status_checked_at'] = 123
            self.assertEqual(build_plan(members, episodes, 1, 'hash')['plan_id'], plan['plan_id'])
            episodes[2]['path'] = '/changed/b.mkv'
            self.assertNotEqual(build_plan(members, episodes, 1, 'hash')['plan_id'], plan['plan_id'])

    def test_unsafe_paths_duplicates_symlinks_and_expansion_rejected(self):
        link = zipfile.ZipInfo('E01.srt')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in [[('../E01.srt', SRT)], [('C:\\E01.srt', SRT)],
                        [('E01.srt', SRT), ('e01.srt', SRT)], [(link, b'other')],
                        [('E01.srt', b'x' * (1024 * 1024 + 1))]]:
            with self.subTest(entries=str(entries)[:90]), self.assertRaises(ValueError):
                with open_pack(pack(entries), POLICY):
                    pass

    def test_rar_and_unknown_archives_are_rejected_with_a_conversion_hint(self):
        for payload, hint in ((rar_pack(), '解压'), (b'not an archive', 'ZIP')):
            with self.subTest(payload=payload[:6]), self.assertRaises(ValueError) as error:
                with open_pack(io.BytesIO(payload), POLICY):
                    pass
            self.assertIn(hint, str(error.exception))

    def test_multi_file_selection_matches_episodes_and_streams_once(self):
        files = [FileStorage(io.BytesIO(SRT), filename='Show.S01E02.chs.srt'),
                 FileStorage(io.BytesIO(SRT), filename='Show.S01E01.chs.srt')]
        episodes = [dict(path='/show/E01.mkv', season=1, episode=1),
                    dict(path='/show/E02.mkv', season=1, episode=2)]
        with open_season_source(files, POLICY) as (source, members):
            self.assertEqual([name for name, _ in members], ['Show.S01E02.chs.srt', 'Show.S01E01.chs.srt'])
            plan = build_plan(members, episodes, 1, 'hash')
            self.assertEqual([row['target']['path'] for row in plan['rows']], ['/show/E02.mkv', '/show/E01.mkv'])
            for _, entry in members:
                with source.open(entry) as reader:
                    self.assertEqual(reader.read(), SRT)

    def test_multi_file_selection_rejects_duplicates_formats_and_oversize(self):
        cases = [([('E01.srt', SRT), ('e01.srt', SRT)], POLICY),
                 ([('E01.rar', SRT)], POLICY),
                 ([('E01.srt', SRT)], dict(POLICY, text_file_limit_mb=0)),
                 ([('E01.srt', SRT), ('E02.srt', SRT)], dict(POLICY, batch_limit_mb=0))]
        for entries, policy in cases:
            with self.subTest(entries=str(entries)[:40]), self.assertRaises(ValueError):
                with open_season_source([FileStorage(io.BytesIO(content), filename=name)
                                         for name, content in entries], policy):
                    pass

    def test_single_zip_file_still_uses_the_archive_path(self):
        with open_season_source([FileStorage(pack([('E01.srt', SRT)]), filename='show.zip')], POLICY) as (archive, members):
            self.assertEqual(members[0][0], 'E01.srt')
            with archive.open(members[0][1]) as reader:
                self.assertEqual(reader.read(), SRT)

    def test_nested_folders_and_non_subtitle_files(self):
        with open_pack(pack([('Season 2/chs/E03.srt', SRT), ('readme.txt', b'note')]), POLICY) as (archive, members):
            self.assertEqual(len(members), 1)
            self.assertEqual(archive.read(members[0][1]), SRT)


class SeasonRouteTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'web/main.py').read_text())
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_upload_season_pack')
        self.scope = dict(os=os, json=json, RMT_MEDIAEXT=['.mkv'],
                          _subtitle_task_owner=lambda: 'user',
                          _subtitle_task_response=lambda task, reused, msg: dict(code=0, task=task))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<route>', 'exec'), self.scope)
        self.call = self.scope['_upload_season_pack']
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.episodes = []
        for episode in (1, 2):
            media = str(Path(self.temp.name) / f'S01E{episode:02}.mkv')
            Path(media).write_bytes(b'media')
            self.episodes.append(dict(season=1, episode=episode, path=media, server_item_id=str(episode)))
        self.scope.update(MediaLibrary=lambda: types.SimpleNamespace(get_episodes=lambda data: dict(code=0, items=self.episodes)),
                          _get_all_media_library_root_paths=lambda: [self.temp.name],
                          _path_authorization_snapshot=lambda media, roots: _path_snapshot(media, roots[0]))
        self.data = pack([('S01E01.chs.srt', SRT), ('S01E02.chs.srt', SRT), ('S02E01.srt', SRT)]).getvalue()
        self.manager = Mock()
        self.captured = None

        def submit(**kwargs):
            self.captured = kwargs
            self.captured['contents'] = [file.read() for file in kwargs['files']]
            return {'task_id': 'season-task'}, False
        self.manager.submit_upload.side_effect = submit

    def invoke(self, mode, files=None, **extra):
        form = MultiDict(dict(upload_mode=mode, item_id='show', season='1', server='emby', request_id='same'))
        form.update(extra)
        self.scope['request'] = types.SimpleNamespace(form=form)
        if files is None:
            files = [FileStorage(io.BytesIO(self.data), filename='show.zip')]
        return self.call(self.manager, POLICY, files)

    def test_preview_no_writes_then_confirm_server_resolved_targets(self):
        preview = self.invoke('season_preview')
        self.manager.submit_upload.assert_not_called()
        self.assertEqual(len([row for row in preview['rows'] if row['target']]), 2)
        ret = self.invoke('season_submit', plan_id=preview['plan_id'], members='["0","1"]')
        self.assertEqual(ret['code'], 0)
        self.assertEqual(self.captured['contents'], [SRT, SRT])
        targets = self.captured['payload']['season_targets']
        self.assertEqual({target['canonical_media_file'] for target in targets.values()}, {row['path'] for row in self.episodes})
        self.assertTrue(all(target['path_authorization'] for target in targets.values()))
        request_id = self.captured['request_id']
        self.invoke('season_submit', plan_id=preview['plan_id'], members='["0","1"]')
        self.assertEqual(self.captured['request_id'], request_id)

    def test_stale_plan_unmatched_selection_and_untrusted_target_fail_before_submission(self):
        preview = self.invoke('season_preview')
        self.assertEqual(self.invoke('season_submit', plan_id='stale', members='["0"]')[1], 409)
        self.assertEqual(self.invoke('season_submit', plan_id=preview['plan_id'], members='["2"]')[1], 400)
        self.scope['_path_authorization_snapshot'] = lambda *args: None
        self.assertEqual(self.invoke('season_submit', plan_id=preview['plan_id'], members='["0"]')[1], 409)
        self.manager.submit_upload.assert_not_called()

    def test_many_selections_fit_one_multipart_field_and_respect_limits(self):
        preview = self.invoke('season_preview')
        with patch.dict(POLICY, season_max_batch_items=1):
            self.assertEqual(self.invoke('season_submit', plan_id=preview['plan_id'], members='["0","1"]')[1], 413)
        self.manager.submit_upload.assert_not_called()

    def test_multi_file_upload_previews_and_submits_only_selected_files(self):
        files = [FileStorage(io.BytesIO(SRT), filename='Show.S01E01.chs.srt'),
                 FileStorage(io.BytesIO(SRT + b'extra'), filename='Show.S01E02.chs.srt'),
                 FileStorage(io.BytesIO(SRT), filename='Show.S02E01.srt')]
        preview = self.invoke('season_preview', files=files)
        self.assertEqual([row['reason'] for row in preview['rows']],
                         ['', '', '季编号与所选季不一致'])
        selected = [row['id'] for row in preview['rows'] if row['target']]
        self.assertEqual(len(selected), 2)
        ret = self.invoke('season_submit', files, plan_id=preview['plan_id'], members=json.dumps(selected))
        self.assertEqual(ret['code'], 0)
        self.assertEqual(self.captured['contents'], [SRT, SRT + b'extra'])
        self.assertEqual(sorted(file.filename for file in self.captured['files']),
                         ['000-Show.S01E01.chs.srt', '001-Show.S01E02.chs.srt'])
        # A modified payload must invalidate the preview instead of silently remapping.
        changed = list(files[:1]) + [FileStorage(io.BytesIO(SRT), filename='Show.S01E02.chs.srt')]
        self.assertEqual(self.invoke('season_submit', changed, plan_id=preview['plan_id'],
                                     members=json.dumps(selected))[1], 409)


class SeasonWorkerTest(unittest.TestCase):
    def test_two_episode_publication_and_recovery_keep_correct_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            media = [str(Path(folder) / f'S01E{episode:02}.mkv') for episode in (1, 2)]
            staged = [str(Path(folder) / f'upload{episode}.srt') for episode in (1, 2)]
            for path in media:
                Path(path).write_bytes(b'media')
            for path in staged:
                Path(path).write_bytes(SRT)
            manager = _UploadManager(media[0], staged[0])
            targets = {}
            manager.items = []
            for index, path in enumerate(media):
                name = f'S01E{index + 1:02}.srt'
                auth = _path_snapshot(path)
                targets[name] = dict(canonical_media_file=path, media_file=path,
                                     path_authorization=dict(source=auth, target=auth))
                manager.items.append(dict(item_id=str(index), item_key=str(index), source_name=name,
                                          staged_path=staged[index], status='queued'))
            manager.task['payload']['season_targets'] = targets
            def update(task_id, item_id, **values):
                item = next(item for item in manager.items if item['item_id'] == item_id)
                item.update(values)
                return item
            manager.update_item = update
            with patch.object(processors, '_update_media_status_after_mutation') as snapshot, \
                    patch.object(processors, '_localized_refresh', return_value=dict(status='succeeded')) as refresh, \
                    patch.object(processors.MediaLibrary, 'invalidate_subtitle_directory_cache'):
                result = processors.process_upload_task(manager, 'upload-task')
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(result['result']['success_count'], 2)
                self.assertEqual([call.args[0] for call in snapshot.call_args_list], media)
                self.assertEqual([call.args[1] for call in refresh.call_args_list], media)
                for path in media:
                    self.assertTrue(Path(path.replace('.mkv', '.eng.srt')).is_file())
                # Simulate recovered publish checkpoints lacking the new result field.
                for item in manager.items:
                    item['result'].pop('canonical_media_file', None)
                manager.verify_upload_item_output = lambda *args: True
                snapshot.reset_mock()
                with patch.object(processors.Subtitle(), 'process_staged_upload') as publish:
                    result = processors.process_upload_task(manager, 'upload-task')
                    publish.assert_not_called()
                self.assertEqual([call.args[0] for call in snapshot.call_args_list], media)
                self.assertEqual(result['status'], 'succeeded')

class SeasonManagerTest(unittest.TestCase):
    def test_full_season_limit_and_mapping_participate_in_deduplication(self):
        from tests.test_subtitle_tasks import _MemoryDb, _File
        from app.helper.subtitle_tasks import SubtitleTaskManager, TaskUploadTooLarge
        with tempfile.TemporaryDirectory() as folder:
            db = _MemoryDb()
            db.init_db()
            manager = SubtitleTaskManager(db=db, staging_root=os.path.join(folder, 'staging'))
            first = str(Path(folder) / 'S01E01.mkv')
            second = str(Path(folder) / 'S01E02.mkv')
            Path(first).write_bytes(b'media')
            Path(second).write_bytes(b'media')
            names = [f'S01E{n:02}.srt' for n in range(1, 25)]
            target = dict(canonical_media_file=first)
            payload = dict(canonical_media_file=first, season_targets={name: dict(target) for name in names})
            with patch.object(manager, 'start'):
                task, reused = manager.submit_upload('owner', [_File(name, SRT) for name in names], payload)
                self.assertFalse(reused)
                self.assertEqual(len(manager.list_items(task['task_id'])), 24)
                again, reused = manager.submit_upload('owner', [_File(name, SRT) for name in names], payload)
                self.assertTrue(reused)
                self.assertEqual(again['task_id'], task['task_id'])
                payload['season_targets'][names[-1]] = dict(canonical_media_file=second)
                changed, reused = manager.submit_upload('owner', [_File(name, SRT) for name in names], payload)
                self.assertFalse(reused)
                self.assertNotEqual(changed['task_id'], task['task_id'])
                with self.assertRaises(TaskUploadTooLarge):
                    manager.submit_upload('owner', [_File(name, SRT) for name in names], dict(canonical_media_file=first))
            db.session.close()
            db.engine.dispose()

    def test_archive_members_open_sequentially_and_close_on_eof(self):
        from app.helper.subtitle_season_pack import MemberStream
        archive = Mock()
        readers = [io.BytesIO(b'one'), io.BytesIO(b'two')]
        archive.open.side_effect = readers
        streams = [MemberStream(archive, 'first'), MemberStream(archive, 'second')]
        archive.open.assert_not_called()
        self.assertEqual(streams[0].read(10), b'one')
        self.assertEqual(streams[0].read(10), b'')
        self.assertTrue(readers[0].closed)
        self.assertEqual(archive.open.call_count, 1)
        self.assertEqual(streams[1].read(10), b'two')
        streams[1].close()
        self.assertTrue(readers[1].closed)


if __name__ == '__main__':
    unittest.main()
