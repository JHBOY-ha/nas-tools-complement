"""Safety regressions for review fixes, using temporary SQL/files and offline CLI."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from sqlalchemy import inspect

from tests.test_database_governance import DatabaseCase
import app.db as database
import app.db.main_db as main_db
import app.db.media_db as media_db
from app.db import backup, runtime
from app.db.models import Base, BaseMedia, SUBTITLETASK, SUBTITLEAUDITSTATE
from app.db.transactions import DatabaseBusy, DatabaseWriteError

ROOT = Path(__file__).resolve().parents[1]


class DatabaseReviewFixTest(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.addCleanup(self.release_runtime)

    def release_runtime(self):
        runtime._prepared.pop(str(self.root), None)
        descriptor = runtime._leases.pop(str(self.root), None)
        if descriptor is not None:
            os.close(descriptor)

    def legacy(self):
        """Use the supported real downgrade, without a Git-history dependency."""
        from alembic.config import Config
        from alembic.command import stamp, downgrade
        self.task('preserve')
        cfg = Config()
        cfg.set_main_option('script_location', str(ROOT / 'db_scripts'))
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                cfg.attributes['connection'] = connection
                stamp(cfg, 'head')
                downgrade(cfg, 'f3b7c1d9e204')

    @contextmanager
    def migration_context(self):
        runtime._prepared[str(self.root)] = {'complete': False}
        cfg = NS(get_config_path=lambda: str(self.root), get_root_path=lambda: str(ROOT))
        with patch.object(database, 'Config', return_value=cfg), \
                patch.object(main_db, '_Database', self.db.managed), \
                patch.object(runtime, 'complete'):
            yield

    def drop_audit_query_indexes(self, names):
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                for name in names:
                    connection.exec_driver_sql('DROP INDEX IF EXISTS "' + name + '"')

    def audit_query_index_names(self):
        return {
            'INDX_SUBTITLE_AUDIT_STATE_SERVER_UPDATED',
            'INDX_SUBTITLE_AUDIT_STATE_SERVER_PATH_UPDATED',
            'INDX_SUBTITLE_AUDIT_STATE_PATH'
        }

    def documented_query_index_names(self):
        # Cover the B0 list as well as the three audit-state indexes that were
        # previously the only ordinary indexes repaired during startup.
        return self.audit_query_index_names() | {
            'INDX_SUBTITLE_TASK_QUEUE',
            'INDX_SUBTITLE_TASK_CREATED',
            'INDX_SUBTITLE_TASK_FINISHED',
            'INDX_SUBTITLE_PROBE_CACHE_PAIR',
            'INDX_TRANSFER_HISTORY_DATE',
        }

    def test_db_persist_return_values_do_not_use_truthiness_as_rollback(self):
        @main_db.DbPersist(self.db)
        def no_change():
            return None

        @main_db.DbPersist(self.db)
        def row_count_zero():
            return 0

        @main_db.DbPersist(self.db)
        def empty_result():
            return []

        with self.db.write_transaction():
            self.db.insert(SUBTITLETASK(ID='before-values', TYPE='audit', STATUS='queued',
                                       CREATED_AT=1, UPDATED_AT=1))
            self.assertTrue(no_change())
            self.assertEqual(row_count_zero(), 0)
            self.assertEqual(empty_result(), [])
            self.db.insert(SUBTITLETASK(ID='after-values', TYPE='audit', STATUS='queued',
                                       CREATED_AT=1, UPDATED_AT=1))
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 2)

    def test_missing_query_indexes_are_repaired_without_business_digest(self):
        names = self.audit_query_index_names()
        self.task('keep-record')
        self.drop_audit_query_indexes(names)
        with self.migration_context(), \
                patch.object(database, '_logical_states', side_effect=AssertionError(
                    'index-only startup must not hash business rows')) as hashes, \
                patch.object(database, 'alembic_upgrade') as upgrade:
            database.update_db()
            upgrade.assert_not_called()
            hashes.assert_not_called()
        with self.db.managed.maintenance() as connection:
            self.assertTrue(database._schema_matches(connection, versioned=True))
            self.assertTrue(database._query_indexes_match(connection))
        self.assertEqual(self.db.query(SUBTITLETASK).one().ID, 'keep-record')

    def test_index_only_boot_does_not_create_migration_backup(self):
        names = self.audit_query_index_names()
        self.task('keep-boot-record')
        self.drop_audit_query_indexes(names)
        with self.migration_context(), \
                patch.object(database.MediaDb, 'init_db'), \
                patch.object(database.MainDb, 'init_db'), \
                patch('app.db.backup.migration_backup') as backup_copy, \
                patch.object(database, '_logical_states', side_effect=AssertionError(
                    'index-only boot must not hash business rows')):
            database.init_db()
            backup_copy.assert_not_called()
        with self.db.managed.maintenance() as connection:
            self.assertTrue(database._query_indexes_match(connection))
        self.assertEqual(self.db.query(SUBTITLETASK).one().ID, 'keep-boot-record')

    def test_each_documented_missing_query_index_is_repaired(self):
        names = self.documented_query_index_names()
        for name in sorted(names):
            self.drop_audit_query_indexes({name})
            with self.migration_context():
                database.update_db()
            with self.db.managed.maintenance() as connection:
                self.assertTrue(database._query_indexes_match(connection))

        self.drop_audit_query_indexes(names)
        with self.migration_context():
            database.update_db()
        with self.db.managed.maintenance() as connection:
            self.assertTrue(database._query_indexes_match(connection))
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)

    def test_same_name_wrong_query_index_definition_stops_startup(self):
        name = 'INDX_SUBTITLE_AUDIT_STATE_PATH'
        self.drop_audit_query_indexes({name})
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                connection.exec_driver_sql(
                    'CREATE INDEX "%s" ON SUBTITLE_AUDIT_STATE (SERVER)' % name)
        with self.migration_context(), self.assertRaisesRegex(DatabaseWriteError, name):
            database.update_db()
        with self.db.managed.maintenance() as connection:
            actual = next(value for value in inspect(connection).get_indexes('SUBTITLE_AUDIT_STATE')
                          if value['name'] == name)
            self.assertEqual(actual['column_names'], ['SERVER'])

    def test_same_name_case_only_partial_index_predicate_drift_stops_startup(self):
        name = 'INDX_SUBTITLE_TASK_INTERACTIVE_CLAIM'
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                connection.exec_driver_sql('DROP INDEX IF EXISTS "' + name + '"')
                connection.exec_driver_sql(
                    "CREATE INDEX \"%s\" ON SUBTITLE_TASK "
                    "(PRIORITY DESC, CREATED_AT, ID) "
                    "WHERE TYPE IN ('UPLOAD','REPAIR') "
                    "AND STATUS IN ('QUEUED','RECOVERING')" % name)
        with self.migration_context(), self.assertRaisesRegex(DatabaseWriteError, 'WHERE'):
            database.update_db()

    def test_query_index_repair_failure_rolls_back_and_is_not_reported_as_success(self):
        name = 'INDX_SUBTITLE_AUDIT_STATE_PATH'
        self.drop_audit_query_indexes({name})
        target = next(index for index in SUBTITLEAUDITSTATE.__table__.indexes if index.name == name)
        with self.migration_context(), \
                patch.object(target, 'create', side_effect=RuntimeError('injected index failure')):
            with self.assertRaisesRegex(DatabaseWriteError, '查询索引补建失败.*injected index failure'):
                database.update_db()
        with self.db.managed.maintenance() as connection:
            self.assertFalse(database._query_indexes_match(connection))

    def test_repeated_startup_does_not_recreate_valid_query_indexes(self):
        target = next(index for index in SUBTITLEAUDITSTATE.__table__.indexes
                      if index.name == 'INDX_SUBTITLE_AUDIT_STATE_PATH')
        with self.migration_context(), patch.object(target, 'create', wraps=target.create) as create:
            database.update_db()
            database.update_db()
        create.assert_not_called()

    def test_query_index_validation_uses_one_reflection_snapshot(self):
        with self.db.managed.maintenance() as connection, \
                patch.object(database, 'inspect', wraps=inspect) as reflected:
            self.assertTrue(database._query_indexes_match(connection))
        self.assertEqual(reflected.call_count, 1)

    def test_every_explicit_model_index_is_in_the_startup_contract(self):
        expected = {
            index.name for table in Base.metadata.tables.values()
            for index in table.indexes
        }
        actual = {index.name for index in database._query_index_definitions()}
        self.assertEqual(actual, expected)

    def test_actual_migration_still_hashes_before_and_after(self):
        self.legacy()
        with self.migration_context(), patch.object(database, '_logical_states', wraps=database._logical_states) as hashes:
            database.update_db()
            self.assertEqual(hashes.call_count, 2)
        self.assertEqual(self.db.query(SUBTITLETASK).one().ID, 'preserve')
        with self.db.managed.maintenance() as connection:
            self.assertTrue(database._schema_matches(connection, versioned=True))

    def test_migration_payload_change_rolls_back_schema_and_version(self):
        self.legacy()
        real_upgrade = database.alembic_upgrade
        def corrupt(cfg, revision):
            real_upgrade(cfg, revision)
            cfg.attributes['connection'].exec_driver_sql("UPDATE SUBTITLE_TASK SET MESSAGE='changed'")
        with self.migration_context(), patch.object(database, 'alembic_upgrade', side_effect=corrupt):
            with self.assertRaisesRegex(DatabaseWriteError, '逻辑数据核对失败'):
                database.update_db()
        with self.db.managed.maintenance() as connection:
            self.assertEqual(connection.exec_driver_sql('SELECT version_num FROM alembic_version').scalar(), 'f3b7c1d9e204')
            self.assertIsNone(connection.exec_driver_sql('SELECT MESSAGE FROM SUBTITLE_TASK').scalar())
            self.assertFalse(database._schema_matches(connection, versioned=True))

    def test_changed_source_creates_new_snapshot_and_preserves_old(self):
        # Existing deployments may link config.yaml; fingerprinting must not
        # add a new startup rejection for previously supported configuration.
        config = self.root / 'linked-config.yaml'
        config.write_text('app: {}\n')
        (self.root / 'config.yaml').symlink_to(config)
        self.task('old')
        first = Path(backup.migration_backup(self.root, self.db.settings))
        self.assertEqual(str(first), backup.migration_backup(self.root, self.db.settings))
        self.task('new')
        second = Path(backup.migration_backup(self.root, self.db.settings))
        self.assertNotEqual(first, second)
        with sqlite3.connect(first / 'user.db') as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM SUBTITLE_TASK').fetchone()[0], 1)
        with sqlite3.connect(second / 'user.db') as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM SUBTITLE_TASK').fetchone()[0], 2)

    def test_incomplete_copy_is_bounded_and_requires_explicit_cleanup(self):
        with patch.object(backup, 'online_backup', side_effect=DatabaseWriteError('injected copy failure')) as copy:
            for _ in range(3):
                with self.assertRaises(DatabaseWriteError):
                    backup.migration_backup(self.root, self.db.settings)
            self.assertEqual(copy.call_count, 1)
        targets = list((self.root / '.db-upgrades').iterdir())
        self.assertEqual(len(targets), 1)
        backup.delete_incomplete_upgrade(self.root, targets[0].name)
        completed = Path(backup.migration_backup(self.root, self.db.settings))
        self.assertTrue((completed / 'manifest.json').exists())
        with self.assertRaises(DatabaseWriteError):
            backup.delete_incomplete_upgrade(self.root, completed.name)

    def test_tampered_deduplicated_backup_is_never_reused(self):
        path = Path(backup.migration_backup(self.root, self.db.settings))
        (path / 'user.db').write_bytes(b'not a database')
        with self.assertRaises(DatabaseWriteError):
            backup.migration_backup(self.root, self.db.settings)
        self.assertEqual(len(list((self.root / '.db-upgrades').iterdir())), 1)

    def staged_restore(self):
        self.task('original')
        archive = backup.create_backup(self.root, self.db.settings)
        backup.stage_restore(archive, self.root, self.db.settings)
        return self.root / '.db-restore-pending.json'

    def test_cancel_interruption_is_resumed_without_applying_snapshot(self):
        marker = self.staged_restore()
        self.task('after-backup')
        # A crash between durable cancellation and deletion must never cause
        # the next startup to apply the now-canceled old snapshot.
        with patch.object(backup.shutil, 'rmtree', side_effect=OSError('injected cleanup failure')):
            with self.assertRaises(OSError):
                backup.cancel_pending_restore(self.root)
        self.assertTrue(json.loads(marker.read_text())['canceled'])
        self.assertFalse(backup.apply_pending_restore(self.root, self.db.settings))
        self.assertFalse(marker.exists())
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 2)
        self.assertEqual(backup.cancel_pending_restore(self.root)['code'], 0)

    def test_cancel_after_directory_deleted_finishes_marker_cleanup(self):
        marker = self.staged_restore()
        value = json.loads(marker.read_text())
        value['canceled'] = True
        backup._write_json(marker, value)
        backup.shutil.rmtree(self.root / '.db-restores' / value['directory'])
        self.assertFalse(backup.apply_pending_restore(self.root, self.db.settings))
        self.assertFalse(marker.exists())

    def test_started_restore_cannot_be_canceled(self):
        marker = self.staged_restore()
        value = json.loads(marker.read_text())
        value.update(rollback='old-snapshot', applied=[])
        backup._write_json(marker, value)
        before = marker.read_bytes()
        with self.assertRaisesRegex(DatabaseWriteError, '恢复已开始'):
            backup.cancel_pending_restore(self.root)
        self.assertEqual(marker.read_bytes(), before)

    def test_admin_paths_cannot_escape_or_follow_symlinks(self):
        marker = self.staged_restore()
        for name in ('../user.db', 'user.db', '/tmp/backup.zip'):
            with self.assertRaises(DatabaseWriteError):
                backup.delete_backup_archive(self.root, name)
        path = self.root / 'backup_file' / ('bk_20261006000000-' + 'a' * 32 + '.zip')
        path.symlink_to(self.db.path)
        with self.assertRaises(DatabaseWriteError):
            backup.delete_backup_archive(self.root, path.name)
        value = json.loads(marker.read_text())
        value['directory'] = '../backup_file'
        backup._write_json(marker, value)
        with self.assertRaises(DatabaseWriteError):
            backup.cancel_pending_restore(self.root)
        self.assertTrue(Path(self.db.path).is_file())

    def cli(self, *args):
        (self.root / 'config.yaml').write_text('app: {}\nmedia: {}\npt: {}\n')
        return subprocess.run([sys.executable, '-m', 'scripts.database_maintenance',
                               '--config-dir', str(self.root), *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def test_offline_cli_lists_cancels_and_deletes_named_archive(self):
        self.staged_restore()
        name = next((self.root / 'backup_file').glob('*.zip')).name
        listing = self.cli('list')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(name, listing.stdout)
        self.assertEqual(self.cli('cancel-restore').returncode, 0)
        self.assertFalse((self.root / '.db-restore-pending.json').exists())
        self.assertEqual(self.cli('delete-backup', name).returncode, 0)
        self.assertFalse((self.root / 'backup_file' / name).exists())

    def test_offline_cli_refuses_running_instance_without_changing_marker(self):
        marker = self.staged_restore()
        before = marker.read_bytes()
        runtime.acquire_instance(self.root)
        result = self.cli('cancel-restore')
        self.assertEqual(result.returncode, 1)
        self.assertIn('已有数据库服务运行', result.stdout)
        self.assertEqual(marker.read_bytes(), before)

    @contextmanager
    def sync_context(self):
        from tests.test_deepseek_audit_reproduction import extracted_function
        with self.db.managed.maintenance() as connection:
            BaseMedia.metadata.create_all(connection)
        fn = extracted_function((ROOT / 'app/mediaserver/media_server.py').read_text(),
                                'sync_mediaserver', {'lock': threading.Lock(), 'log': Mock()})
        with patch.object(media_db, '_Database', self.db.managed):
            store = media_db.MediaDb()
            store.insert('emby', dict(id='old', type='Movie'))
            store.statistics('emby', 1, 1, 0)
            receiver = NS(server=True, mediadb=store, progress=Mock(), _server_type=NS(value='emby'),
                          get_medias_count=lambda: dict(MovieCount=2, SeriesCount=0),
                          get_libraries=lambda: [dict(id='lib', name='Library')],
                          get_items=lambda _id: [dict(id='a', type='Movie'), dict(id='b', type='Movie')])
            yield fn, receiver, store

    def read_sync(self):
        with sqlite3.connect(self.db.path) as connection:
            return (connection.execute('SELECT ITEM_ID FROM MEDIASYNC_ITEMS ORDER BY ITEM_ID').fetchall(),
                    # Legacy statistics columns are Text; preserve that storage
                    # contract while comparing their numeric totals here.
                    int(connection.execute('SELECT TOTAL_COUNT FROM MEDIASYNC_STATISTICS').fetchone()[0]))

    def test_sync_success_hides_partial_rows_and_fetches_without_writer_slot(self):
        with self.sync_context() as (sync, receiver, store):
            def items(_id):
                self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)
                return [dict(id='a', type='Movie'), dict(id='b', type='Movie')]
            receiver.get_items = items
            insert = store.insert
            def insert_and_observe(*args):
                result = insert(*args)
                self.assertEqual(self.read_sync(), ([('old',)], 1))
                return result
            with patch.object(store, 'insert', side_effect=insert_and_observe):
                self.assertTrue(sync(receiver))
            self.assertEqual(self.read_sync(), ([('a',), ('b',)], 2))
            receiver.progress.end.assert_called_once_with('mediasync')

    def test_sync_capacity_or_statistics_failure_preserves_rows_and_progress(self):
        with self.sync_context() as (sync, receiver, store):
            for failure in (patch.object(self.db.managed, '_check_capacity', side_effect=DatabaseBusy('full')),
                            patch.object(store, 'statistics', return_value=False),
                            patch.object(receiver, 'get_items', side_effect=RuntimeError('provider failure'))):
                receiver.progress.reset_mock()
                with failure:
                    self.assertFalse(sync(receiver))
                self.assertEqual(self.read_sync(), ([('old',)], 1))
                receiver.progress.end.assert_called_once_with('mediasync')

    def test_sync_empty_snapshot_can_commit_without_dividing_by_zero(self):
        with self.sync_context() as (sync, receiver, _store):
            receiver.get_medias_count = lambda: dict(MovieCount=0, SeriesCount=0)
            receiver.get_items = lambda _id: []
            self.assertTrue(sync(receiver))
            self.assertEqual(self.read_sync(), ([], 0))
