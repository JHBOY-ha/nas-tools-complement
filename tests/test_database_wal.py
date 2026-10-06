"""Mandatory WAL acceptance on a fixed runtime; unsupported runtimes fail, not skip."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select, event

from tests.test_database_governance import DatabaseCase
from app.db.models import SUBTITLETASK, SUBTITLEAUDITSTATE, SUBTITLEPUBLICATION
from app.db.publication import publish_now, abort_unfinished, collect
from app.db.settings import DatabaseSettings, wal_runtime_supported
from app.db.runtime import probe_wal, acquire_instance, validate_database
from app.db.backup import online_backup
from app.db.transactions import DatabaseBusy
from app.helper.subtitle_media_status import SubtitleMediaStatusStore


class DatabaseWalTest(DatabaseCase):
    def setUp(self):
        self.assertTrue(wal_runtime_supported(sqlite3.sqlite_version_info),
                        'WAL acceptance requires a fixed SQLite runtime; do not skip these tests')
        super().setUp()
        with self.db.managed.maintenance() as connection:
            self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode=WAL').scalar(), 'wal')

    def test_two_process_probe_preserves_reader_snapshot(self):
        self.assertTrue(probe_wal(self.root))

    def test_existing_wal_refuses_unsafe_volume_runtime_and_failed_probe(self):
        from app.db.runtime import prepare
        from app.db.transactions import DatabaseWriteError
        conditions = (patch('app.db.runtime.filesystem_type', return_value='nfs'),
                      patch('app.db.runtime.sqlite3.sqlite_version_info', (3, 51, 2)),
                      patch('app.db.runtime.probe_wal', return_value=False))
        for condition in conditions:
            with condition, self.assertRaises(DatabaseWriteError):
                prepare(self.root, DatabaseSettings(reserve_free_mb=256))
        # No fallback journal switch or sidecar deletion happened to the
        # already-WAL database when bootstrap rejected its environment.
        with self.db.managed.maintenance() as connection:
            self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode').scalar(), 'wal')

    def test_retained_read_snapshot_does_not_block_writer(self):
        complete, failures = threading.Event(), []
        with self.db.read_snapshot():
            self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)
            def write():
                try:
                    self.task('concurrent')
                except Exception as error:
                    failures.append(error)
                finally:
                    self.db.remove_session()
                    complete.set()
            thread = threading.Thread(target=write); thread.start()
            self.assertTrue(complete.wait(3), 'WAL writer must finish while the read snapshot remains open')
            self.assertEqual(failures, [])
            self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)
        thread.join(3)
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 1)

    def test_online_backup_includes_commits_still_in_wal(self):
        with self.db.managed.maintenance() as connection:
            connection.exec_driver_sql('PRAGMA wal_autocheckpoint=0')
        with self.db.read_snapshot():
            self.db.query(SUBTITLETASK).count()
            thread = threading.Thread(target=lambda: self.task('wal-only'))
            thread.start(); thread.join(3)
            self.assertFalse(thread.is_alive())
            target = self.root / 'wal-snapshot.db'
            online_backup(self.db.path, target)
            with sqlite3.connect(target) as snapshot:
                self.assertEqual(snapshot.execute('SELECT ID FROM SUBTITLE_TASK').fetchone()[0], 'wal-only')
            validate_database(target)

    def test_online_backup_during_writes_preserves_multirow_transaction_consistency(self):
        self.task('pair-a'); self.task('pair-b')
        started, stop = threading.Event(), threading.Event()
        commits, failures = [], []
        def update():
            try:
                number = 0
                while not stop.is_set():
                    number += 1
                    with self.db.write_transaction():
                        self.db.query(SUBTITLETASK).update({'MESSAGE': str(number)})
                    commits.append(number)
                    started.set()
                    time.sleep(.001)
            except BaseException as error:
                failures.append(error)
            finally:
                self.db.remove_session()
        worker = threading.Thread(target=update); worker.start()
        target = self.root / 'concurrent-snapshot.db'
        try:
            self.assertTrue(started.wait(5))
            before = len(commits)
            online_backup(self.db.path, target)
            self.assertGreater(len(commits), before, 'Backup must overlap actual committed writes')
        finally:
            stop.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        validate_database(target)
        with sqlite3.connect(target) as snapshot:
            messages = [row[0] for row in snapshot.execute('SELECT MESSAGE FROM SUBTITLE_TASK')]
        self.assertEqual(len(messages), 2)
        # Both rows are modified in one transaction; a torn direct file copy
        # must never produce mixed values in the accepted snapshot.
        self.assertEqual(len(set(messages)), 1)

    def test_chunked_media_reads_pin_one_publication_generation(self):
        store = SubtitleMediaStatusStore(self.db)
        paths = ['/media/chunk-%s.mkv' % i for i in range(501)]
        store.upsert_many('emby', [dict(media_path=path, status='old') for path in paths])
        self.db.remove_session()
        first, published = threading.Event(), threading.Event()
        outcomes = []
        def update():
            try:
                self.assertTrue(first.wait(5))
                store.upsert_many('emby', [dict(media_path=path, status='new') for path in paths])
            except BaseException as error:
                outcomes.append(error)
            finally:
                self.db.remove_session(); published.set()
        def hold_first(_connection, _cursor, statement, _values, _context, _many):
            if statement.startswith('SELECT') and 'SUBTITLE_MEDIA_STATUS' in statement and not first.is_set():
                first.set()
                self.assertTrue(published.wait(5))
        event.listen(self.db.engine, 'after_cursor_execute', hold_first)
        worker = threading.Thread(target=update); worker.start()
        try:
            rows = store.list_for_paths('emby', paths)
        finally:
            event.remove(self.db.engine, 'after_cursor_execute', hold_first)
            worker.join(5)
        self.assertEqual(outcomes, [])
        self.assertEqual(len(rows), 501)
        self.assertEqual({row['status'] for row in rows.values()}, {'old'})
        self.assertEqual(store.get('emby', paths[-1])['status'], 'new')
        self.assertEqual(self.db.managed.read_diagnostics()['active'], 0)

    def test_killed_checkpoint_keeps_acknowledged_writes_recoverable(self):
        # A historical reader holds TRUNCATE in its busy wait, so the killed
        # child is genuinely inside SQLite checkpoint rather than after it.
        import select as ready
        with self.db.read_snapshot():
            self.db.query(SUBTITLETASK).count()
            worker = threading.Thread(target=lambda: self.task('checkpoint-ack'))
            worker.start(); worker.join(3)
            self.assertFalse(worker.is_alive())
            code = '''
import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=30)
c.execute('PRAGMA synchronous=FULL')
c.set_trace_callback(lambda sql: print('checkpoint',flush=True) if 'wal_checkpoint' in sql else None)
c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
'''
            child = subprocess.Popen([sys.executable, '-c', code, self.db.path],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(ready.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline().strip(), 'checkpoint')
                time.sleep(.05)
                self.assertIsNone(child.poll())
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
            self.assertNotEqual(child.returncode, 0)
        self.assertEqual(self.db.query(SUBTITLETASK).one().ID, 'checkpoint-ack')
        validate_database(self.db.path)

    def test_long_reader_high_water_rejects_then_recovers_without_deleting_wal(self):
        self.db.managed.settings = DatabaseSettings(reserve_free_mb=256, wal_warning_mb=1, wal_limit_mb=2)
        with self.db.managed.maintenance() as connection:
            connection.exec_driver_sql('PRAGMA wal_autocheckpoint=0')
        with self.db.read_snapshot():
            self.db.query(SUBTITLETASK).count()
            errors = []
            def append():
                try:
                    with self.db.write_transaction():
                        self.db.insert(SUBTITLETASK(ID='large', TYPE='audit', OWNER='x', STATUS='queued',
                            PAYLOAD='x' * (3 * 1024 * 1024), CREATED_AT=1, UPDATED_AT=1))
                except Exception as error:
                    errors.append(error)
                finally:
                    self.db.remove_session()
            thread = threading.Thread(target=append); thread.start(); thread.join(4)
            self.assertEqual(errors, [])
            wal = Path(self.db.path + '-wal')
            self.assertGreater(wal.stat().st_size, 2 * 1024 * 1024)
            def reject():
                try:
                    with self.db.write_transaction():
                        self.fail('High-water writes must not execute')
                except DatabaseBusy:
                    errors.append('rejected')
                finally:
                    self.db.remove_session()
            thread = threading.Thread(target=reject); thread.start(); thread.join(3)
            self.assertEqual(errors, ['rejected'])
            self.assertTrue(wal.exists())
        self.task('after-reader')
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 2)

    def test_second_process_cannot_acquire_service_instance_lease(self):
        acquire_instance(self.root)
        code = 'from app.db.runtime import acquire_instance; import sys; acquire_instance(sys.argv[1])'
        result = subprocess.run([sys.executable, '-c', code, str(self.root)],
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已有数据库服务运行', result.stderr.decode('utf-8'))

    def test_process_crash_after_stage_or_publish_recovers_one_consistent_generation(self):
        # os._exit models abrupt application death, not physical power loss.
        for phase in ('batch', 'before_publish', 'published'):
            with self.subTest(phase=phase):
                task_id = 'crash-' + phase
                self.task(task_id)
                publish_now(self.db, audit_rows=self.audit_rows(status='old', scope=task_id))
                self.db.remove_session()
                code = '''
import os,sys
from tests.test_database_governance import FileDatabase
from app.helper.subtitle_tasks import SubtitleTaskManager
db=FileDatabase(sys.argv[1])
manager=SubtitleTaskManager(db=db,staging_root=sys.argv[1]+'.staging')
manager._publication_hook=lambda phase,identifier: os._exit(23) if phase==sys.argv[3] else None
manager.commit_audit_result(sys.argv[2],sys.argv[2],'emby',
    {'/media/%s.mkv'%i:{'status':'new'} for i in range(1001)}, {}, 'succeeded','done')
'''
                result = subprocess.run([sys.executable, '-c', code, self.db.path, task_id, phase],
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 23, result.stderr.decode('utf-8'))
                self.db.remove_session()
                row = self.db.query(SUBTITLETASK).filter_by(ID=task_id).one()
                states = self.db.query(SUBTITLEAUDITSTATE).filter_by(SCOPE_KEY=task_id).all()
                if phase == 'published':
                    self.assertEqual(row.STATUS, 'succeeded')
                    self.assertEqual(len(states), 1001)
                    self.assertEqual({state.STATUS for state in states}, {'new'})
                else:
                    self.assertEqual(row.STATUS, 'running')
                    self.assertEqual(len(states), 1)
                    self.assertEqual(states[0].STATUS, 'old')
                abort_unfinished(self.db)
                collect(self.db)
                validate_database(self.db.path)
