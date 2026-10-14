"""Real independent SQLite connections; no production config, NAS or network."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import tests.test_subtitle_tasks  # existing optional-dependency fixtures
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import QueuePool

from app.db.models import (Base, SUBTITLETASK, SUBTITLEAUDITSTATE, SUBTITLEMEDIASTATUS,
                           SUBTITLEPUBLICATION, SUBTITLESTATECLOCK)
from app.db.publication import (seed_legacy, filter_visible, begin, stage, publish,
                                publish_now, collect, sequence)
from app.db.settings import DatabaseSettings, wal_runtime_supported
from app.db.transactions import (ManagedDatabase, WriteCoordinator, DatabaseBusy,
                                 DatabaseWriteError)
from app.db.backup import online_backup, create_backup, stage_restore, apply_pending_restore
from app.db.runtime import validate_database
from app.helper.subtitle_tasks import SubtitleTaskManager
from app.helper.subtitle_media_status import SubtitleMediaStatusStore


class FileDatabase:
    """Production transaction/visibility protocol with a disposable file path."""
    def __init__(self, path, coordinator=None):
        self.path = str(path)
        self.settings = DatabaseSettings(reserve_free_mb=256)
        self.engine = create_engine('sqlite:///' + self.path, poolclass=QueuePool,
                                    pool_size=8, max_overflow=16,
                                    connect_args={'check_same_thread': False})
        writer = create_engine('sqlite:///' + self.path, poolclass=QueuePool,
                                pool_size=1, max_overflow=0,
                                connect_args={'check_same_thread': False})
        self.managed = ManagedDatabase(self.engine, writer, self.settings,
                                       coordinator or WriteCoordinator(self.settings), self.path)
        self.init_db()

    def init_db(self):
        with self.managed.maintenance() as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                Base.metadata.create_all(connection)
                seed_legacy(connection)

    @property
    def session(self):
        return self.managed.session

    def query(self, *objects):
        return filter_visible(self.session.query(*objects), objects)

    def insert(self, row):
        self.session.add(row)

    def commit(self):
        self.managed.commit()

    def rollback(self):
        self.managed.rollback()

    def remove_session(self):
        self.managed.remove_session()

    def write_transaction(self, required_bytes=0):
        return self.managed.write_transaction(required_bytes=required_bytes)

    def read_snapshot(self):
        return self.managed.read_snapshot()

    def close(self):
        self.remove_session()
        self.engine.dispose()
        self.managed.write_engine.dispose()


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='db-governance-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = FileDatabase(self.root / 'user.db')
        self.addCleanup(self.db.close)

    def task(self, name='audit'):
        with self.db.write_transaction():
            self.db.insert(SUBTITLETASK(ID=name, TYPE='audit', OWNER='owner', STATUS='running',
                                        CREATED_AT=time.time(), UPDATED_AT=time.time()))

    def audit_rows(self, count=1, status='ok', scope='scope'):
        now = time.time()
        return [dict(SCOPE_KEY=scope, SERVER='emby', SUBTITLE_PATH='/media/%s.mkv' % index,
                     MEDIA_PATH='/media/%s.mkv' % index, STATUS=status, RESULT=json.dumps({'status': status}),
                     CONFIRMED_AT=now, UPDATED_AT=now) for index in range(count)]


class DatabaseGovernanceTest(DatabaseCase):
    def test_fixed_runtime_matrix_does_not_accept_intermediate_unpatched_versions(self):
        for version in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 1)):
            self.assertTrue(wal_runtime_supported(version))
        for version in ((3, 37, 2), (3, 44, 5), (3, 45, 0), (3, 49, 9), (3, 50, 6), (3, 51, 2)):
            self.assertFalse(wal_runtime_supported(version))

    def test_settings_reject_unsafe_mode_and_validate_counts(self):
        with self.assertRaises(ValueError):
            DatabaseSettings.from_config({'database': {'journal_mode': 'off'}})
        value = DatabaseSettings.from_config({'database': {'writer_queue_size': True, 'audit_batch_rows': 0}})
        self.assertEqual(value.writer_queue_size, 64)
        self.assertEqual(value.audit_batch_rows, 1000)
        with self.assertRaises(ValueError):
            DatabaseSettings.from_config({'database': {'wal_warning_mb': 256, 'wal_limit_mb': 64}})

    def test_connection_pragmas_are_verified_and_reader_cannot_write(self):
        with self.db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('PRAGMA synchronous').scalar(), 2)
            self.assertEqual(connection.exec_driver_sql('PRAGMA foreign_keys').scalar(), 1)
            self.assertEqual(connection.exec_driver_sql('PRAGMA busy_timeout').scalar(), 30000)
            self.assertEqual(connection.exec_driver_sql('PRAGMA query_only').scalar(), 1)
            with self.assertRaises(Exception):
                connection.exec_driver_sql("DELETE FROM SUBTITLE_TASK")
            with self.assertRaises(DatabaseWriteError):
                connection.exec_driver_sql('PRAGMA wal_checkpoint(PASSIVE)')
            for statement in ('/* reader */ PRAGMA query_only(OFF)',
                              '-- reader\nPRAGMA query_only=OFF', 'BEGIN IMMEDIATE'):
                with self.subTest(statement=statement), self.assertRaises(DatabaseWriteError):
                    connection.exec_driver_sql(statement)
            self.assertEqual(connection.exec_driver_sql('PRAGMA query_only').scalar(), 1)

    def test_unadmitted_orm_changes_cannot_be_committed(self):
        self.db.insert(SUBTITLETASK(ID='rogue', TYPE='audit', OWNER='x', STATUS='queued',
                                   CREATED_AT=1, UPDATED_AT=1))
        with self.assertRaises(DatabaseWriteError):
            self.db.commit()
        self.db.remove_session()
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)

    def test_nested_failure_cannot_be_swallowed_and_partially_committed(self):
        with self.assertRaises(DatabaseWriteError):
            with self.db.write_transaction():
                self.db.insert(SUBTITLETASK(ID='nested', TYPE='audit', OWNER='x', STATUS='queued',
                                           CREATED_AT=1, UPDATED_AT=1))
                try:
                    with self.db.write_transaction():
                        raise RuntimeError('inner')
                except RuntimeError:
                    pass
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)

    def test_two_files_share_one_writer_without_cross_file_atomicity(self):
        other = FileDatabase(self.root / 'media.db', self.db.managed.coordinator)
        self.addCleanup(other.close)
        entered, release = threading.Event(), threading.Event()
        errors, order = [], []
        def first():
            try:
                with self.db.write_transaction():
                    order.append('first')
                    entered.set()
                    release.wait(3)
            except Exception as error:
                errors.append(error)
        def second():
            try:
                with other.write_transaction():
                    order.append('second')
            except Exception as error:
                errors.append(error)
        one = threading.Thread(target=first); one.start()
        self.assertTrue(entered.wait(2))
        two = threading.Thread(target=second); two.start()
        deadline = time.monotonic() + 2
        while self.db.managed.coordinator.snapshot()['waiting'] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 1)
        self.assertEqual(order, ['first'])
        release.set(); one.join(3); two.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(order, ['first', 'second'])
        with self.db.write_transaction():
            with self.assertRaises(DatabaseWriteError):
                with other.write_transaction():
                    pass

    def test_reservation_counts_toward_capacity_without_holding_writer(self):
        coordinator = WriteCoordinator(DatabaseSettings(writer_queue_size=1, reserve_free_mb=256))
        with coordinator.reserve():
            self.assertEqual(coordinator.snapshot()['active'], 0)
            failures = []
            def compete():
                try:
                    with coordinator.slot(object()):
                        pass
                except DatabaseBusy:
                    failures.append(True)
            thread = threading.Thread(target=compete); thread.start(); thread.join(2)
            self.assertEqual(failures, [True])
            with coordinator.slot(self.db):
                self.assertEqual(coordinator.snapshot()['reserved'], 1)
            # The metadata permit survives multiple file-record transactions.
            with coordinator.slot(self.db):
                self.assertEqual(coordinator.snapshot()['active'], 1)
        self.assertEqual(coordinator.snapshot()['active'], 0)

    def test_hidden_batches_are_not_visible_until_one_publication(self):
        publish_now(self.db, audit_rows=self.audit_rows(status='old'))
        identifier, rows, _ = begin(self.db, self.audit_rows(status='new'))
        with self.db.write_transaction():
            stage(self.db, identifier, SUBTITLEAUDITSTATE, rows)
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).one().STATUS, 'old')
        with self.db.write_transaction():
            publish(self.db, identifier)
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).one().STATUS, 'new')

    def test_audit_batches_keep_old_states_and_terminal_until_publish(self):
        self.task()
        publish_now(self.db, audit_rows=self.audit_rows(status='old'))
        store = SubtitleMediaStatusStore(db=self.db)
        store.upsert_many('emby', [{'media_path': '/media/0.mkv', 'has_external': False}])
        manager = SubtitleTaskManager(db=self.db, staging_root=str(self.root / 'staging'))
        observed = []
        def checkpoint(phase, _identifier):
            if phase in ('building', 'batch', 'before_publish'):
                with self.db.read_snapshot():
                    observed.append((phase, manager.get_audit_states('scope', 'emby')['/media/0.mkv']['status'],
                                     store.get('emby', '/media/0.mkv')['has_external'],
                                     self.db.query(SUBTITLETASK).filter_by(ID='audit').one().STATUS))
        manager._publication_hook = checkpoint
        manager.commit_audit_result('audit', 'scope', 'emby',
                                    {'/media/%s.mkv' % i: {'status': 'new'} for i in range(2001)},
                                    {}, 'succeeded', 'done', media_snapshots=[
                                        {'media_path': '/media/0.mkv', 'has_external': True}])
        self.assertTrue(any(item[0] == 'batch' for item in observed))
        self.assertTrue(all(item[1:] == ('old', False, 'running') for item in observed))
        self.assertEqual(manager.get_audit_states('scope', 'emby')['/media/0.mkv']['status'], 'new')
        self.assertTrue(store.get('emby', '/media/0.mkv')['has_external'])
        self.assertEqual(self.db.query(SUBTITLETASK).one().STATUS, 'succeeded')

    def test_cancel_between_batches_does_not_publish_partial_new_state(self):
        self.task()
        publish_now(self.db, audit_rows=self.audit_rows(status='old'))
        manager = SubtitleTaskManager(db=self.db, staging_root=str(self.root / 'staging'))
        def checkpoint(phase, _identifier):
            if phase == 'batch':
                manager.cancel_task('audit', owner='owner')
        manager._publication_hook = checkpoint
        answer = manager.commit_audit_result('audit', 'scope', 'emby',
                                             {'/media/%s.mkv' % i: {'status': 'new'} for i in range(1001)},
                                             {}, 'succeeded', 'done')
        self.assertEqual(answer['status'], 'canceled')
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).one().STATUS, 'old')

    def test_final_commit_failure_rolls_back_marker_and_task_together(self):
        self.task()
        publish_now(self.db, audit_rows=self.audit_rows(status='old'))
        manager = SubtitleTaskManager(db=self.db, staging_root=str(self.root / 'staging'))
        armed = {'value': False}
        def commit_failure(_connection):
            if armed['value']:
                armed['value'] = False
                raise RuntimeError('commit failed before SQLite acknowledgement')
        event.listen(self.db.managed.write_engine, 'commit', commit_failure)
        self.addCleanup(event.remove, self.db.managed.write_engine, 'commit', commit_failure)
        manager._publication_hook = lambda phase, _identifier: armed.update(value=True) if phase == 'before_publish' else None
        with self.assertRaises(RuntimeError):
            manager.commit_audit_result('audit', 'scope', 'emby', {'/media/0.mkv': {'status': 'new'}},
                                         {}, 'succeeded', 'done')
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).one().STATUS, 'old')
        self.assertEqual(self.db.query(SUBTITLETASK).one().STATUS, 'running')
        validate_database(self.db.path)

    def test_replace_and_incremental_publications_keep_distinct_semantics(self):
        publish_now(self.db, audit_rows=self.audit_rows(2))
        publish_now(self.db, audit_rows=self.audit_rows(status='updated'))
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).count(), 2)
        publish_now(self.db, audit_rows=self.audit_rows(status='replace'),
                    server='emby', scope_key='scope', replace=True)
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).count(), 1)
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).one().STATUS, 'replace')
        collect(self.db)
        self.assertEqual(self.db.query(SUBTITLEAUDITSTATE).count(), 1)

    def test_tombstone_and_collection_never_resurrect_old_media(self):
        store = SubtitleMediaStatusStore(db=self.db)
        store.upsert_many('emby', [{'media_path': '/media/0.mkv', 'has_external': False}])
        store.upsert_many('emby', [{'media_path': '/media/0.mkv', 'has_external': True}])
        before = sequence(self.db)
        self.assertEqual(store.delete_paths('emby', ['/media/0.mkv']), 1)
        collect(self.db)
        self.assertIsNone(store.get('emby', '/media/0.mkv'))
        store.upsert_many('emby', [{'media_path': '/media/1.mkv', 'has_external': True}])
        self.assertGreater(sequence(self.db), before)

    def test_native_foreign_key_error_is_redacted_and_slot_released(self):
        with self.assertRaises(DatabaseWriteError) as context:
            with self.db.write_transaction():
                self.db.insert(SUBTITLEAUDITSTATE(PUBLICATION_ID='secret-api-key',
                    SCOPE_KEY='scope', SERVER='emby', SUBTITLE_PATH='/secret-path',
                    STATUS='ok', CONFIRMED_AT=1, UPDATED_AT=1))
        self.assertNotIn('secret-api-key', str(context.exception))
        self.assertNotIn('/secret-path', str(context.exception))
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)
        validate_database(self.db.path)

    def test_low_space_rejects_before_sql_and_preserves_visible_data(self):
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')(1000, 999, 1)
        with patch('app.db.runtime.shutil.disk_usage', return_value=usage):
            with self.assertRaises(DatabaseBusy):
                with self.db.write_transaction():
                    self.fail('Rejected admission must not execute its callback')
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)

    def test_batch_space_budget_preserves_margin_before_begin(self):
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')(1024 ** 3, 0, (256 + 10) * 1024 ** 2)
        with patch('app.db.runtime.shutil.disk_usage', return_value=usage):
            with self.assertRaises(DatabaseBusy):
                with self.db.write_transaction(required_bytes=20 * 1024 ** 2):
                    self.fail('Payload budget must be checked before running SQL')
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)

    def test_waiter_timeout_does_not_release_active_writer_or_leak_queue(self):
        coordinator = WriteCoordinator(DatabaseSettings(writer_queue_size=1, writer_wait_seconds=1))
        owner, queued = object(), object()
        outcomes = []
        def wait():
            try:
                with coordinator.slot(queued):
                    outcomes.append('unexpected admission')
            except DatabaseBusy:
                outcomes.append('timed out')
        with coordinator.slot(owner):
            worker = threading.Thread(target=wait); worker.start()
            deadline = time.monotonic() + .5
            while coordinator.snapshot()['waiting'] != 1 and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(coordinator.snapshot()['queue_used'], 1)
            rejected = []
            def overflow():
                try:
                    with coordinator.slot(object()):
                        rejected.append('incorrectly admitted')
                except DatabaseBusy:
                    rejected.append('full')
            extra = threading.Thread(target=overflow); extra.start(); extra.join(.5)
            self.assertEqual(rejected, ['full'])
            worker.join(3)
            self.assertEqual(outcomes, ['timed out'])
            self.assertEqual(coordinator.snapshot()['active'], 1)
            self.assertEqual(coordinator.snapshot()['queue_used'], 0)
        self.assertEqual(coordinator.snapshot()['active'], 0)

    def test_clean_reader_is_returned_before_waiting_for_write_admission(self):
        queued, completed = threading.Event(), threading.Event()
        failures = []
        def wait():
            try:
                self.db.query(SUBTITLETASK).count()
                queued.set()
                with self.db.write_transaction():
                    pass
            except BaseException as error:
                failures.append(error)
            finally:
                self.db.remove_session(); completed.set()
        with self.db.write_transaction():
            worker = threading.Thread(target=wait); worker.start()
            self.assertTrue(queued.wait(3))
            deadline = time.monotonic() + 2
            while self.db.managed.coordinator.snapshot()['waiting'] != 1 and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(self.db.managed.coordinator.snapshot()['waiting'], 1)
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
            self.assertFalse(completed.is_set())
        worker.join(3)
        self.assertTrue(completed.is_set())
        self.assertEqual(failures, [])

    def test_native_sqlite_full_rolls_back_without_losing_acknowledged_rows(self):
        self.task('acknowledged')
        with self.db.managed.maintenance() as connection:
            pages = connection.exec_driver_sql('PRAGMA page_count').scalar()
            connection.exec_driver_sql('PRAGMA max_page_count=%d' % (pages + 1))
        with self.assertRaises(DatabaseWriteError):
            with self.db.write_transaction():
                self.db.insert(SUBTITLETASK(ID='too-large', TYPE='audit', OWNER='x', STATUS='queued',
                    PAYLOAD='x' * 1024 * 1024, CREATED_AT=1, UPDATED_AT=1))
        self.assertEqual([row.ID for row in self.db.query(SUBTITLETASK).all()], ['acknowledged'])
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)
        validate_database(self.db.path)

    def test_native_external_lock_failure_is_identifiable_and_releases_slot(self):
        from dataclasses import replace
        self.db.managed.settings = replace(self.db.settings, busy_timeout_seconds=1)
        self.db.managed.write_engine.dispose()
        blocker = sqlite3.connect(self.db.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaises(DatabaseBusy):
                with self.db.write_transaction():
                    self.fail('A rejected native BEGIN must not execute business code')
        finally:
            blocker.rollback(); blocker.close()
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)
        self.assertEqual(self.db.managed.write_engine.pool.checkedout(), 0)
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)

    def test_sqlite_write_permission_failure_never_acknowledges_success(self):
        # Native query_only injects SQLite's permission failure consistently,
        # including CI running as root where chmod would not deny writes.
        with self.db.managed.maintenance() as connection:
            connection.exec_driver_sql('PRAGMA query_only=ON')
        try:
            with self.assertRaises(DatabaseWriteError):
                with self.db.write_transaction():
                    self.db.insert(SUBTITLETASK(ID='denied', TYPE='audit', OWNER='x', STATUS='queued',
                                               CREATED_AT=1, UPDATED_AT=1))
        finally:
            with self.db.managed.maintenance() as connection:
                connection.exec_driver_sql('PRAGMA query_only=OFF')
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)

    def test_move_publication_commit_failure_retains_source_and_inode_evidence(self):
        from app.media.meta.extra_transfer import publish_exclusive
        from app.utils.types import RmtMode
        from sqlalchemy.exc import OperationalError
        source, destination = self.root / 'source.mkv', self.root / 'destination.mkv'
        source.write_bytes(b'owned bytes')
        def transfer(origin, target, _mode):
            shutil.copyfile(origin, target)
            return 0
        def record():
            self.db.insert(SUBTITLETASK(ID='file-record', TYPE='audit', OWNER='x', STATUS='queued',
                                       CREATED_AT=1, UPDATED_AT=1))
            return True
        def fail(_connection):
            raise OperationalError('COMMIT', {'token': 'private-value'}, sqlite3.OperationalError('disk I/O error'))
        with patch('app.db.main_db._Database', self.db.managed):
            event.listen(self.db.managed.write_engine, 'commit', fail)
            try:
                with self.assertRaises(DatabaseWriteError) as error:
                    publish_exclusive(str(source), str(destination), RmtMode.MOVE, transfer, record)
            finally:
                event.remove(self.db.managed.write_engine, 'commit', fail)
            self.assertNotIn('private-value', str(error.exception))
            self.assertEqual(source.read_bytes(), b'owned bytes')
            self.assertEqual(destination.read_bytes(), b'owned bytes')
            receipts = list(self.root.glob('destination.mkv.*.extra-pending'))
            self.assertEqual(len(receipts), 1)
            self.assertTrue(os.path.samefile(receipts[0], destination))
            self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)
            # Replace externally after the failed commit. A later attempt must
            # preserve that external inode rather than trusting stale ownership.
            external = self.root / 'external'; external.write_bytes(b'external bytes')
            os.replace(external, destination)
            with self.assertRaises(ValueError):
                publish_exclusive(str(source), str(destination), RmtMode.MOVE, transfer, record)
            self.assertEqual(destination.read_bytes(), b'external bytes')
            self.assertEqual(source.read_bytes(), b'owned bytes')

    def test_distinct_publications_overlap_without_pinning_metadata_workers(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.media.meta import extra_transfer
        from app.utils.isolated_io import get_io_pool
        from app.utils.types import RmtMode
        from app.utils.workload import FairConcurrencyGate
        barrier = threading.Barrier(3)
        release = threading.Event()
        def transfer(source, target, _mode):
            barrier.wait(3)
            release.wait(3)
            shutil.copyfile(source, target)
            return 0
        def run(number):
            source = self.root / ('source-%s' % number)
            source.write_bytes(b'media')
            extra_transfer.publish_exclusive(str(source), str(self.root / ('target-%s' % number)),
                                             RmtMode.COPY, transfer, lambda: True)
        with patch('app.db.main_db._Database', self.db.managed), \
                patch.object(extra_transfer, 'get_transfer_gate', return_value=FairConcurrencyGate(2)), \
                ThreadPoolExecutor(2) as executor:
            jobs = [executor.submit(run, number) for number in range(2)]
            try:
                # Both independent transfers must enter before either completes.
                barrier.wait(3)
                self.assertEqual(get_io_pool().execute('path_query', query='isdir',
                                                      path=str(self.root), timeout=1), True)
            finally:
                release.set()
            for job in jobs:
                job.result(5)
        self.assertEqual(extra_transfer._publish_locks, {})

    def test_same_destination_waiters_serialize_and_release_lock_entries(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.media.meta import extra_transfer
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        destination = str(self.root / 'same-target')
        def first():
            with extra_transfer._lock_destination(destination):
                entered.set()
                release.wait(3)
        def second():
            with extra_transfer._lock_destination(destination):
                second_entered.set()
                raise ValueError('test cleanup after failure')
        with ThreadPoolExecutor(2) as executor:
            one = executor.submit(first)
            try:
                self.assertTrue(entered.wait(2))
                two = executor.submit(second)
                # Observe the second registered waiter, not scheduling timing.
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with extra_transfer._publish_locks_guard:
                        waiting = next(iter(extra_transfer._publish_locks.values()))[1] == 2
                    if waiting:
                        break
                    time.sleep(.01)
                self.assertTrue(waiting)
                self.assertFalse(second_entered.is_set())
            finally:
                release.set()
            one.result(3)
            with self.assertRaises(ValueError):
                two.result(3)
        self.assertTrue(second_entered.is_set())
        self.assertEqual(extra_transfer._publish_locks, {})

    def test_file_publication_space_rejection_occurs_before_transfer(self):
        from app.media.meta.extra_transfer import publish_exclusive
        from app.utils.types import RmtMode
        from collections import namedtuple
        source, destination = self.root / 'before.mkv', self.root / 'after.mkv'
        source.write_bytes(b'original')
        usage = namedtuple('usage', 'total used free')(1000, 999, 1)
        with patch('app.db.main_db._Database', self.db.managed), \
                patch('app.db.runtime.shutil.disk_usage', return_value=usage):
            with self.assertRaises(DatabaseBusy):
                publish_exclusive(str(source), str(destination), RmtMode.MOVE,
                    lambda *_args: self.fail('Rejected reservation must not transfer bytes'), lambda: True)
        self.assertFalse(destination.exists())
        self.assertEqual(source.read_bytes(), b'original')

    def test_foreign_key_check_catches_corruption_integrity_check_does_not(self):
        path = self.root / 'bad-foreign.db'
        with sqlite3.connect(path) as connection:
            connection.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); '
                'CREATE TABLE child(id INTEGER REFERENCES parent(id)); INSERT INTO child VALUES(999);')
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        with self.assertRaises(DatabaseWriteError):
            validate_database(path)

    def test_logically_invalid_publication_metadata_is_not_accepted_as_integrity_ok(self):
        with self.db.write_transaction():
            self.db.session.execute(SUBTITLESTATECLOCK.__table__.update().values(SEQUENCE=-1))
        with self.assertRaises(DatabaseWriteError):
            validate_database(self.db.path)
        with self.db.write_transaction():
            self.db.session.execute(SUBTITLESTATECLOCK.__table__.update().values(SEQUENCE=0))
            self.db.insert(SUBTITLEPUBLICATION(ID='invalid', STATUS='published', SEQUENCE=None,
                                              CREATED_AT=1, UPDATED_AT=1))
        # Both corruptions satisfy SQLite's structural constraints, so the
        # application-level clock/visibility checks must explicitly reject them.
        with self.assertRaises(DatabaseWriteError):
            validate_database(self.db.path)

    def test_auto_preserves_delete_and_explicit_wal_rejects_unsupported_volume(self):
        from app.db.runtime import prepare, _prepared
        with patch('app.db.runtime.filesystem_type', return_value='nfs'):
            value = prepare(self.root, DatabaseSettings(reserve_free_mb=256))
            self.assertEqual(value['target'], 'delete')
            _prepared.pop(str(self.root.resolve()))
            with self.assertRaises(DatabaseWriteError):
                prepare(self.root, DatabaseSettings(journal_mode='wal', reserve_free_mb=256))
        with sqlite3.connect(self.db.path) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')

    def test_partial_claim_index_uses_fixed_predicate_and_no_sort(self):
        with self.db.write_transaction():
            self.db.session.execute(SUBTITLETASK.__table__.insert(), [dict(
                ID='queue-%s' % i, TYPE='upload', OWNER='x', STATUS='queued' if i < 10 else 'succeeded',
                PRIORITY=i % 3, CREATED_AT=float(i), UPDATED_AT=float(i)) for i in range(5000)])
        with self.db.managed.maintenance() as connection:
            connection.exec_driver_sql('ANALYZE')
            plan = [row[3] for row in connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT * FROM SUBTITLE_TASK INDEXED BY INDX_SUBTITLE_TASK_INTERACTIVE_CLAIM "
                "WHERE TYPE IN ('upload','repair') "
                "AND STATUS IN ('queued','recovering') ORDER BY PRIORITY DESC,CREATED_AT ASC,ID ASC LIMIT 1")]
        self.assertTrue(any('INTERACTIVE_CLAIM' in item for item in plan), plan)
        self.assertFalse(any('TEMP B-TREE' in item for item in plan), plan)


class DatabaseBackupTest(DatabaseCase):
    """Backup/restore tests share the real store fixture without fixture bypasses."""
    def _archive(self, entries):
        path = self.root / ('archive-%s.zip' % time.time_ns())
        with zipfile.ZipFile(path, 'w') as bundle:
            for name, content in entries:
                bundle.writestr(name, content)
        return path

    def test_online_backup_restores_committed_data_and_private_permissions(self):
        self.task('snapshot')
        target = self.root / 'snapshot.db'
        online_backup(self.db.path, target)
        validate_database(target)
        with sqlite3.connect(target) as connection:
            self.assertEqual(connection.execute('SELECT ID FROM SUBTITLE_TASK').fetchone()[0], 'snapshot')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_restore_stages_without_changing_active_database_and_rejects_second(self):
        self.task('old')
        archive = create_backup(self.root, DatabaseSettings(reserve_free_mb=256))
        with self.db.write_transaction():
            self.db.query(SUBTITLETASK).filter_by(ID='old').update({'MESSAGE': 'current'})
        answer = stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))
        self.assertTrue(answer['restart_required'])
        self.assertEqual(self.db.query(SUBTITLETASK).one().MESSAGE, 'current')
        with self.assertRaises(DatabaseBusy):
            stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))
        self.db.close()
        apply_pending_restore(self.root, DatabaseSettings(reserve_free_mb=256))
        validate_database(self.db.path)
        with sqlite3.connect(self.db.path) as connection:
            self.assertIsNone(connection.execute('SELECT MESSAGE FROM SUBTITLE_TASK').fetchone()[0])

    def test_restore_rejects_traversal_symlink_duplicate_and_corrupt_database(self):
        raw = Path(self.db.path).read_bytes()
        for entries in ([('user.db', raw), ('../escape', b'bad')],
                        [('user.db', raw), ('user.db', raw)], [('user.db', b'corrupt')]):
            with self.subTest(names=[name for name, _value in entries]):
                archive = self._archive(entries)
                with self.assertRaises(Exception):
                    stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))
                self.assertFalse((self.root / '.db-restore-pending.json').exists())
        link = zipfile.ZipInfo('user.db')
        link.create_system = 3; link.external_attr = (stat.S_IFLNK | 0o777) << 16
        path = self.root / 'symlink.zip'
        with zipfile.ZipFile(path, 'w') as bundle:
            bundle.writestr(link, '/etc/passwd')
        with self.assertRaises(DatabaseWriteError):
            stage_restore(path, self.root, DatabaseSettings(reserve_free_mb=256))

    def test_legacy_zip_without_manifest_or_media_is_accepted_and_validated(self):
        self.task('legacy')
        archive = self._archive([('user.db', Path(self.db.path).read_bytes())])
        answer = stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))
        self.assertEqual(answer['code'], 0)

    def test_manifest_change_is_rejected_without_touching_source(self):
        archive = self._archive([('user.db', Path(self.db.path).read_bytes()),
                                 ('manifest.json', json.dumps({'format': 1, 'files': {'user.db': 'wrong'}}))])
        before = hashlib.sha256(Path(self.db.path).read_bytes()).hexdigest()
        with self.assertRaises(DatabaseWriteError):
            stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))
        self.assertEqual(hashlib.sha256(Path(self.db.path).read_bytes()).hexdigest(), before)

    def test_restore_rejects_invalid_configuration_and_symlinked_private_root(self):
        archive = self._archive([('user.db', Path(self.db.path).read_bytes()), ('config.yaml', b'app: [broken')])
        with self.assertRaises(Exception):
            stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))
        private = self.root / '.db-restores'
        private.rmdir()
        external = self.root / 'elsewhere'; external.mkdir()
        private.symlink_to(external, target_is_directory=True)
        archive = self._archive([('user.db', Path(self.db.path).read_bytes())])
        with self.assertRaises(DatabaseWriteError):
            stage_restore(archive, self.root, DatabaseSettings(reserve_free_mb=256))

    def test_concurrent_restore_cannot_overwrite_another_pending_journal(self):
        import app.db.backup as backup
        archive = self._archive([('user.db', Path(self.db.path).read_bytes())])
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        validate = backup._validate_restore
        def delayed(target):
            entered.set()
            self.assertTrue(release.wait(5))
            validate(target)
        def first():
            try:
                outcomes.append(stage_restore(archive, self.root, self.db.settings))
            except BaseException as error:
                outcomes.append(error)
        with patch.object(backup, '_validate_restore', side_effect=delayed):
            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(DatabaseBusy):
                    stage_restore(archive, self.root, self.db.settings)
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertTrue(outcomes[0]['restart_required'])

    def test_interrupted_offline_restore_resumes_and_preserves_original_snapshots(self):
        import app.db.backup as backup
        other = FileDatabase(self.root / 'media.db', self.db.managed.coordinator)
        self.addCleanup(other.close)
        self.task('restored')
        with other.write_transaction():
            other.insert(SUBTITLETASK(ID='media', TYPE='audit', OWNER='x', STATUS='queued',
                                      MESSAGE='saved', CREATED_AT=1, UPDATED_AT=1))
        (self.root / 'config.yaml').write_text('app: {}\n')
        archive = create_backup(self.root, self.db.settings)
        with self.db.write_transaction():
            self.db.query(SUBTITLETASK).update({'MESSAGE': 'before restore'})
        with other.write_transaction():
            other.query(SUBTITLETASK).update({'MESSAGE': 'before restore'})
        stage_restore(archive, self.root, self.db.settings)
        self.db.close(); other.close()
        write_json = backup._write_json
        def fail_after_first_replace(path, value):
            if value.get('applied') == ['user.db']:
                raise OSError('simulated interruption before receipt')
            write_json(path, value)
        with patch.object(backup, '_write_json', side_effect=fail_after_first_replace):
            with self.assertRaises(OSError):
                apply_pending_restore(self.root, self.db.settings)
        journal = json.loads((self.root / '.db-restore-pending.json').read_text())
        rollback = Path(journal['rollback'])
        self.assertTrue((rollback / 'config.yaml').is_file())
        with sqlite3.connect(rollback / 'user.db') as connection:
            self.assertEqual(connection.execute('SELECT MESSAGE FROM SUBTITLE_TASK').fetchone()[0], 'before restore')
        # A replacement without a saved receipt is safe to repeat at the next
        # offline startup. No business connection is opened between files.
        self.assertTrue(apply_pending_restore(self.root, self.db.settings))
        self.assertFalse((self.root / '.db-restore-pending.json').exists())
        for name in ('user.db', 'media.db'):
            validate_database(self.root / name)
        with sqlite3.connect(self.root / 'media.db') as connection:
            self.assertEqual(connection.execute('SELECT MESSAGE FROM SUBTITLE_TASK').fetchone()[0], 'saved')

    def test_tampered_restore_receipt_never_bypasses_original_backup(self):
        archive = self._archive([('user.db', Path(self.db.path).read_bytes())])
        stage_restore(archive, self.root, self.db.settings)
        marker = self.root / '.db-restore-pending.json'
        original = json.loads(marker.read_text())
        for fields in ({'format': 99}, {'rollback': '/tmp'}, {'applied': ['user.db']},
                       {'applied': ['unknown.db']}, {'applied': ['user.db', 'user.db']}):
            marker.write_text(json.dumps(dict(original, **fields)))
            with self.subTest(fields=fields), self.assertRaises(DatabaseWriteError):
                apply_pending_restore(self.root, self.db.settings)
        self.assertFalse((self.root / '.db-upgrades').exists())
