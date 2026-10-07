"""Check the supplied DeepSeek review and regressions for the six selected fixes.

Run: python3 -m tests.run_stability tests.test_deepseek_audit_reproduction
Cases 01/03/04/06/10/12 now assert fixed behavior; remaining cases document
observed conditions or corrections to the report. Only the six corrected cases
are included in the default regression suite. All files
and SQL writes use disposable directories. External services are mocked.
"""
import ast
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

if os.environ.get('NASTOOL_OFFLINE_TESTS') != '1':
    raise RuntimeError('Use tests.run_deepseek_reproduction or tests.run_stability for isolated config/DBs')

from tests.test_database_governance import FileDatabase
import app.db as database
import app.db.main_db as main_db
import app.db.media_db as media_db
from app.db import runtime
from app.db.backup import create_backup, stage_restore
from app.db.models import BaseMedia, SUBTITLETASK, SUBTITLEAUDITSTATE, TRANSFERHISTORY
from app.db.settings import DatabaseSettings, wal_runtime_supported
from app.db.transactions import DatabaseBusy, DatabaseWriteError

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = {}
MIB = 1024 * 1024


def extracted_function(source, name, namespace):
    """Execute the actual function body while avoiding unrelated app startup."""
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<production-body>', 'exec'), namespace)
    return namespace[name]


class DeepSeekReproduction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='deepseek-repro-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = FileDatabase(self.root / 'user.db')
        self.addCleanup(self.db.close)
        self.addCleanup(self.release_runtime)

    def release_runtime(self):
        # Release only this test's lease; never touch another test/app's state.
        runtime._prepared.pop(str(self.root), None)
        descriptor = runtime._leases.pop(str(self.root), None)
        if descriptor is not None:
            os.close(descriptor)

    @contextmanager
    def update_context(self, prepared=True):
        if prepared:
            # Isolate update_db's migration phase; standalone bootstrap has a
            # separate subprocess test with real configuration and both stores.
            runtime._prepared.setdefault(str(self.root), {'complete': False})
        config = NS(get_config_path=lambda: str(self.root), get_root_path=lambda: str(ROOT))
        with patch.object(database, 'Config', return_value=config), \
                patch.object(main_db, '_Database', self.db.managed):
            yield

    def sql(self, statement):
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                return connection.exec_driver_sql(statement)

    def wrong_offset(self):
        # Preserve all normal columns, changing only the declared legacy type.
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import Integer, Text
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                op = Operations(MigrationContext.configure(connection))
                with op.batch_alter_table('CUSTOM_WORDS', recreate='always') as batch:
                    batch.alter_column('OFFSET', existing_type=Text(), type_=Integer())

    def test_01_low_space_blocks_prepare_and_real_write(self):
        settings = DatabaseSettings()
        self.assertEqual(settings.reserve_free_mb, 256)
        self.assertEqual(DatabaseSettings.from_config({'database': {'reserve_free_mb': 2048}}).reserve_free_mb, 2048)
        threshold = (settings.reserve_free_mb + 4) * MIB
        with patch.object(runtime.shutil, 'disk_usage', return_value=NS(free=threshold - 1)):
            logger = Mock()
            initialize = extracted_function((ROOT / 'run.py').read_text(), 'init_system',
                                            dict(log=logger, APP_VERSION='test', init_db=database.init_db))
            # Expected capacity errors end startup with a clear message, without
            # continuing into services or exposing an unhandled traceback.
            with self.update_context(prepared=False), self.assertRaises(SystemExit) as startup:
                initialize()
            self.assertEqual(startup.exception.code, 1)
            self.assertIn('app.database.reserve_free_mb', str(logger.console.call_args))
            self.db.managed.settings = settings
            with self.assertRaises(DatabaseBusy) as write:
                with self.db.write_transaction():
                    self.fail('A low-space write must not reach its body')
        with patch.object(runtime.shutil, 'disk_usage', return_value=NS(free=threshold)):
            runtime.check_write_capacity(self.db.path, settings)
        EVIDENCE['01'] = dict(threshold_bytes=threshold, startup=str(startup.exception),
                              write=str(write.exception), exact_threshold_accepted=True)

    def test_02_old_sqlite_and_overlay_choose_delete(self):
        cases = []
        for version, filesystem in (((3, 41, 2), 'ext4'), ((3, 51, 3), 'overlay')):
            runtime._prepared.pop(str(self.root), None)
            with patch.object(runtime.sqlite3, 'sqlite_version_info', version), \
                    patch.object(runtime.sqlite3, 'sqlite_version', '.'.join(map(str, version))), \
                    patch.object(runtime, 'filesystem_type', return_value=filesystem), \
                    patch.object(runtime, 'probe_wal') as probe:
                state = runtime.prepare(self.root, DatabaseSettings(reserve_free_mb=256))
                self.assertEqual(state['target'], 'delete')
                probe.assert_not_called()
                cases.append(dict(version=version, filesystem=filesystem, target=state['target']))
        from tests import run_database
        with patch.object(sys, 'argv', ['run_database']), \
                patch.object(sqlite3, 'sqlite_version_info', (3, 41, 2)), redirect_stdout(io.StringIO()):
            self.assertEqual(run_database.main(), 2)
        EVIDENCE['02'] = dict(simulated_gates=cases, acceptance_exit=2,
                              docker_image_executed=False)

    def test_03_matching_schema_skips_payload_hashes(self):
        # A real 100k-row table must not be scanned when no migration is needed.
        with self.db.managed.maintenance() as connection:
            with connection.begin():
                connection.execute(TRANSFERHISTORY.__table__.insert(),
                                   [dict(ID=i + 1, TITLE='test movie', DATE='2026-10-06')
                                    for i in range(100000)])
            self.assertTrue(database._schema_matches(connection, versioned=True))
        original = database._logical_states
        passes = []
        def measure(connection):
            started = time.perf_counter()
            result = original(connection)
            passes.append(dict(seconds=time.perf_counter() - started,
                               history_rows=result['TRANSFER_HISTORY'][0], tables=len(result)))
            return result
        state = {'complete': False}
        runtime._prepared[str(self.root)] = state
        def finish(_directory):
            state['complete'] = True
        with self.update_context(), patch.object(runtime, 'complete', side_effect=finish), \
                patch.object(database, '_logical_states', side_effect=measure):
            database.update_db()
            self.assertEqual(len(passes), 0)
            database.update_db()
            self.assertEqual(len(passes), 0)  # Same-process follow-up is idempotent.
            state['complete'] = False
            database.update_db()
            self.assertEqual(len(passes), 0)
        EVIDENCE['03'] = dict(passes=passes, same_process_repeat_passes=0,
                              completion_hook_mocked=True)

    def test_04_sync_rejection_preserves_previous_cache(self):
        # Only the remote library is fake. The loop, decorators and media SQL
        # are production code, backed by independent real SQLite transactions.
        sync = extracted_function((ROOT / 'app/mediaserver/media_server.py').read_text(),
                                  'sync_mediaserver', {'lock': threading.Lock(), 'log': Mock()})
        with self.db.managed.maintenance() as connection:
            BaseMedia.metadata.create_all(connection)
        outcomes = []
        with patch.object(media_db, '_Database', self.db.managed):
            store = media_db.MediaDb()
            for successful_inserts in (0, 1):
                store.empty()
                store.insert('emby', dict(id='old', title='old', type='Movie'))
                progress = Mock()
                receiver = NS(server=True, mediadb=store, progress=progress,
                              _server_type=NS(value='emby'),
                              get_medias_count=lambda: dict(MovieCount=2, SeriesCount=0),
                              get_libraries=lambda: [dict(id='lib', name='library')],
                              get_items=lambda _id: [dict(id='new1', type='Movie'),
                                                    dict(id='new2', type='Movie')])
                original_insert = store.insert
                calls = []
                def failing_insert(*args):
                    if len(calls) == successful_inserts:
                        raise DatabaseBusy('injected rejection')
                    calls.append(True)
                    return original_insert(*args)
                # Fail after empty() and optionally after a valid insert/flush;
                # the transaction must restore the previous committed rows.
                with patch.object(store, 'insert', side_effect=failing_insert):
                    self.assertFalse(sync(receiver))
                with sqlite3.connect(self.db.path) as connection:
                    ids = [row[0] for row in connection.execute('SELECT ITEM_ID FROM MEDIASYNC_ITEMS')]
                self.assertEqual(ids, ['old'])
                progress.end.assert_called_once_with('mediasync')
                outcomes.append(dict(committed_ids=ids, progress_ended=True))
        EVIDENCE['04'] = outcomes

    def test_05a_unknown_revision_is_rejected_by_current_code(self):
        self.sql('CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)')
        self.sql("INSERT INTO alembic_version VALUES('unknown_future_revision')")
        with self.update_context(), self.assertRaisesRegex(DatabaseWriteError, '版本无法识别'):
            database.update_db()
        EVIDENCE['05a'] = 'Current code rejects an unknown future revision.'

    def test_05b_legacy_update_swallows_unknown_new_revision(self):
        # Use the actual HEAD implementation and revision directory. Skip if
        # HEAD has moved to the new implementation, rather than invent history.
        old = subprocess.check_output(['git', 'show', 'HEAD:app/db/__init__.py'], cwd=ROOT, text=True)
        if 'except Exception as e:' not in old or '_logical_states' in old:
            self.skipTest('HEAD no longer contains the pre-governance update_db')
        legacy_root = self.root / 'legacy'
        scripts = legacy_root / 'db_scripts'
        shutil.copytree(ROOT / 'db_scripts', scripts, ignore=shutil.ignore_patterns('__pycache__'))
        (scripts / 'versions/7e1c9a42b605_database_publications.py').unlink()
        self.sql('CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)')
        self.sql("INSERT INTO alembic_version VALUES('7e1c9a42b605')")
        from alembic.config import Config as AlembicConfig
        from alembic.command import upgrade
        config = NS(get_config_path=lambda: str(self.root), get_root_path=lambda: str(legacy_root))
        call = extracted_function(old, 'update_db', dict(os=os, log=Mock(), Config=lambda: config,
                                  AlembicConfig=AlembicConfig, alembic_upgrade=upgrade))
        output = io.StringIO()
        with redirect_stdout(output):
            call()
        self.assertIn('7e1c9a42b605', output.getvalue())
        EVIDENCE['05b'] = dict(legacy_update_raised=False, output=output.getvalue().strip(),
                               full_old_application_started=False)

    def test_05c_unversioned_integer_offset_stops_before_upgrade(self):
        self.wrong_offset()
        with self.update_context(), patch.object(database, 'alembic_upgrade') as upgrade:
            with self.assertRaisesRegex(DatabaseWriteError, '旧数据库 schema'):
                database.update_db()
            upgrade.assert_not_called()
        EVIDENCE['05c'] = 'Unversioned INTEGER OFFSET rejected before Alembic upgrade.'

    def test_06_failed_boots_reuse_identical_migration_backup(self):
        self.wrong_offset()
        from sqlalchemy import create_engine
        media_engine = create_engine('sqlite:///' + str(self.root / 'media.db'))
        with media_engine.begin() as connection:
            BaseMedia.metadata.create_all(connection)
        media_engine.dispose()
        # Both database files and both config files are synthetic test content.
        with sqlite3.connect(self.root / 'media.db') as connection:
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.execute("INSERT INTO probe VALUES('media evidence')")
        (self.root / 'config.yaml').write_text(
            'app:\n  database:\n    journal_mode: delete\n    reserve_free_mb: 256\nmedia: {}\npt: {}\n')
        (self.root / 'default-category.yaml').write_text('movie: {}\n')
        env = dict(os.environ, NASTOOL_CONFIG=str(self.root / 'config.yaml'), NASTOOL_OFFLINE_TESTS='1')
        self.db.close()
        exits = []
        for _ in range(3):
            result = subprocess.run([sys.executable, '-c', 'from app.db import init_db; init_db()'],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=40)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('旧数据库 schema', result.stderr)
            exits.append(result.returncode)
        backups = list((self.root / '.db-upgrades').iterdir())
        self.assertEqual(len(backups), 1)
        for target in backups:
            self.assertEqual({p.name for p in target.iterdir()},
                             {'user.db', 'media.db', 'config.yaml', 'default-category.yaml', 'manifest.json'})
        EVIDENCE['06'] = dict(failed_boots=exits, backup_count=len(backups),
                              bytes=sum(p.stat().st_size for b in backups for p in b.iterdir()))

    def test_07_false_return_rolls_back_the_enclosing_unit(self):
        @main_db.DbPersist(self.db)
        def reject():
            self.db.insert(SUBTITLETASK(ID='rejected', TYPE='audit', STATUS='queued',
                                       CREATED_AT=1, UPDATED_AT=1))
            self.db.session.flush()  # Prove real valid SQL is undone by False.
            return False
        self.assertFalse(reject())
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)
        with self.assertRaises(DatabaseWriteError):
            with self.db.write_transaction():
                self.db.insert(SUBTITLETASK(ID='outer', TYPE='audit', STATUS='queued',
                                           CREATED_AT=1, UPDATED_AT=1))
                reject()  # Ignoring False still leaves the shared unit doomed.
                self.db.insert(SUBTITLETASK(ID='after', TYPE='audit', STATUS='queued',
                                           CREATED_AT=1, UPDATED_AT=1))
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 0)
        self.assertEqual(self.db.managed.coordinator.snapshot()['active'], 0)
        with self.db.write_transaction():
            self.db.insert(SUBTITLETASK(ID='next', TYPE='audit', STATUS='queued',
                                       CREATED_AT=1, UPDATED_AT=1))
        self.assertEqual(self.db.query(SUBTITLETASK).count(), 1)
        EVIDENCE['07'] = dict(standalone_rolled_back=True, nested_outer_rolled_back=True,
                              ignored_false_rolled_back=True, writer_reusable=True,
                              production_helper_defect_claimed=False)

    @unittest.skipUnless(wal_runtime_supported(sqlite3.sqlite_version_info), 'Requires fixed SQLite for real WAL')
    def test_08_long_reader_blocks_affected_database_writes_until_release(self):
        self.db.managed.settings = replace(self.db.settings, wal_limit_mb=8, wal_warning_mb=4)
        with self.db.managed.maintenance() as connection:
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        ready, release = threading.Event(), threading.Event()
        errors = []
        def reader():
            try:
                with self.db.read_snapshot():
                    self.db.query(SUBTITLETASK).count()
                    ready.set()
                    if not release.wait(15):
                        raise TimeoutError('Test did not release reader')
            except BaseException as error:
                errors.append(repr(error))
                ready.set()
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(errors, [])
            with sqlite3.connect(self.db.path) as writer:
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('CREATE TABLE repro_payload(value BLOB)')
                writer.execute('INSERT INTO repro_payload VALUES(zeroblob(10485760))')
            size = Path(self.db.path + '-wal').stat().st_size
            self.assertGreater(size, 8 * MIB)
            with self.assertRaisesRegex(DatabaseBusy, 'WAL 达到高水位'):
                with self.db.write_transaction():
                    pass
            diagnostic = self.db.managed.read_diagnostics()
            self.assertEqual(diagnostic['active'], 1)
            other = FileDatabase(self.root / 'media.db', coordinator=self.db.managed.coordinator)
            try:
                with other.write_transaction():
                    other.insert(SUBTITLETASK(ID='other', TYPE='audit', STATUS='queued',
                                             CREATED_AT=1, UPDATED_AT=1))
            finally:
                other.close()
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with self.db.write_transaction():
            self.db.insert(SUBTITLETASK(ID='resumed', TYPE='audit', STATUS='queued',
                                       CREATED_AT=1, UPDATED_AT=1))
        EVIDENCE['08'] = dict(wal_bytes=size, diagnostic=diagnostic,
                              other_database_write_succeeded=True, write_resumed=True,
                              test_limit_mb=8, default_limit_mb=256)

    def test_09_reported_missing_indexes_are_in_model_and_migration(self):
        from alembic.command import downgrade, upgrade, stamp
        from alembic.config import Config
        from sqlalchemy import inspect
        names = {'INDX_SUBTITLE_AUDIT_STATE_SERVER_UPDATED',
                 'INDX_SUBTITLE_AUDIT_STATE_SERVER_PATH_UPDATED', 'INDX_SUBTITLE_AUDIT_STATE_PATH'}
        self.assertTrue(names <= {index.name for index in SUBTITLEAUDITSTATE.__table__.indexes})
        cfg = Config()
        cfg.set_main_option('script_location', str(ROOT / 'db_scripts'))
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                cfg.attributes['connection'] = connection
                stamp(cfg, 'head')
                downgrade(cfg, 'f3b7c1d9e204')
                # Remove the exact reported indexes before the real rebuild.
                for name in names:
                    connection.exec_driver_sql('DROP INDEX IF EXISTS "' + name + '"')
                upgrade(cfg, 'head')
                found = {index['name'] for index in inspect(connection).get_indexes('SUBTITLE_AUDIT_STATE')}
                self.assertTrue(names <= found)
                for name in names:
                    connection.exec_driver_sql('DROP INDEX "' + name + '"')
                # Table/constraint matching intentionally ignores ordinary
                # query indexes; the separate checker must still report them.
                self.assertTrue(database._schema_matches(connection, versioned=True))
                self.assertFalse(database._query_indexes_match(connection))
                self.assertEqual(database._ensure_query_indexes(connection), len(names))
                matches_with_repaired = database._query_indexes_match(connection)
                self.assertTrue(matches_with_repaired)
        EVIDENCE['09'] = dict(model_and_upgrade_include=sorted(names),
                              structural_validator_ignores_missing_indexes=True,
                              query_indexes_repaired=matches_with_repaired,
                              task_manager_started=False)

    def test_10_pending_restore_can_be_canceled_and_archive_explicitly_deleted(self):
        from app.db.backup import cancel_pending_restore, delete_backup_archive
        archives = [create_backup(self.root, self.db.settings) for _ in range(3)]
        self.assertEqual(len(list((self.root / 'backup_file').glob('*.zip'))), 3)
        stage_restore(archives[0], self.root, self.db.settings)
        marker = (self.root / '.db-restore-pending.json').read_bytes()
        with self.assertRaisesRegex(DatabaseBusy, '已有待重启恢复'):
            stage_restore(archives[1], self.root, self.db.settings)
        self.assertEqual((self.root / '.db-restore-pending.json').read_bytes(), marker)
        self.assertEqual(len(list((self.root / '.db-restores').iterdir())), 1)
        self.assertEqual(cancel_pending_restore(self.root)['code'], 0)
        self.assertFalse((self.root / '.db-restore-pending.json').exists())
        self.assertEqual(len(list((self.root / '.db-restores').iterdir())), 0)
        self.assertEqual(stage_restore(archives[1], self.root, self.db.settings)['code'], 0)
        delete_backup_archive(self.root, Path(archives[0]).name)
        self.assertEqual(len(list((self.root / 'backup_file').glob('*.zip'))), 2)
        EVIDENCE['10'] = dict(archives=2, staged_restores=1, canceled_then_restaged=True)

    def test_11_missing_baseline_commit_breaks_actual_loaders(self):
        # A repository with no baseline object models an archive checkout or a
        # shallow descendant. Not every shallow clone lacks the baseline HEAD.
        scratch = self.root / 'without-history'
        scratch.mkdir()
        subprocess.run(['git', 'init', '-q', str(scratch)], check=True, capture_output=True)
        from scripts.verify_database_workload import load_baseline
        with self.assertRaises(subprocess.CalledProcessError):
            load_baseline(scratch)
        from tests.test_database_migrations import DatabaseMigrationTest
        case = DatabaseMigrationTest('test_upgrade_preserves_original_ids_values_and_supports_repeat')
        original = subprocess.check_output
        def in_missing_history(*args, **kwargs):
            return original(*args, **dict(kwargs, cwd=scratch))
        try:
            with patch.object(subprocess, 'check_output', side_effect=in_missing_history):
                with self.assertRaises(subprocess.CalledProcessError):
                    case.setUp()
        finally:
            case.doCleanups()
        EVIDENCE['11'] = dict(workload_loader_failed=True, migration_setup_failed=True,
                              trigger='baseline object absent, not shallow status alone')

    def test_12_update_without_prepare_bootstraps_safely(self):
        self.assertNotIn(str(self.root), runtime._prepared)
        (self.root / 'config.yaml').write_text('app:\n  database:\n    journal_mode: delete\nmedia: {}\npt: {}\n')
        self.db.close()
        code = ('from app.db import update_db; from app.db.runtime import _prepared; '
                'update_db(); assert all(s["complete"] for s in _prepared.values()); update_db()')
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                env=dict(os.environ, NASTOOL_CONFIG=str(self.root / 'config.yaml')),
                                text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db.path) as connection:
            revision = connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        self.assertEqual(revision, '7e1c9a42b605')
        EVIDENCE['12'] = dict(exit_code=0, committed_revision=revision, initialized_both_databases=True)
