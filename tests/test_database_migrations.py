"""Upgrade/downgrade real legacy files without rewriting production data."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tests.test_subtitle_tasks
from alembic.config import Config
from alembic.command import upgrade, downgrade, stamp
from sqlalchemy import create_engine

from tests.test_database_governance import FileDatabase
from app.db.models import SUBTITLEAUDITSTATE
from app.db.publication import publish_now, delete_rows
from app.db.runtime import validate_database, prepare
from app.db.settings import DatabaseSettings
from app.db import _logical_states, _schema_matches


class DatabaseMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='db-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'user.db'
        source = subprocess.check_output(['git', 'show', '8d84b13:app/db/models.py'], text=True)
        self.legacy = {'__name__': 'legacy_database_fixture'}
        # Immutable local commit is the schema oracle, not a copy of the new
        # implementation with its constraints removed to make a test pass.
        exec(compile(source, '8d84b13/models.py', 'exec'), self.legacy)
        engine = create_engine('sqlite:///' + str(self.path))
        with engine.begin() as connection:
            self.legacy['Base'].metadata.create_all(connection)
            connection.execute(self.legacy['SUBTITLEAUDITSTATE'].__table__.insert(), [dict(
                ID=41 + i, SCOPE_KEY='scope', SERVER='emby', SUBTITLE_PATH='/media/%s.mkv' % i,
                STATUS='legacy', RESULT='{"status":"legacy"}', CONFIRMED_AT=1.25, UPDATED_AT=1.25)
                for i in range(2)])
            connection.execute(self.legacy['SUBTITLEMEDIASTATUS'].__table__.insert(), dict(
                ID=77, SERVER='emby', MEDIA_PATH='/media/0.mkv', HAS_EXTERNAL=1,
                CHECKED_AT=1.5, UPDATED_AT=1.5))
        engine.dispose()
        self.db = FileDatabase(self.path)
        self.addCleanup(self.db.close)
        self.config = Config()
        self.config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'db_scripts'))
        self.config.set_main_option('sqlalchemy.url', 'sqlite:///' + str(self.path))

    def migrate(self, command, revision):
        self.db.remove_session()
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                self.config.attributes['connection'] = connection
                command(self.config, revision)

    def test_upgrade_preserves_original_ids_values_and_supports_repeat(self):
        with self.db.managed.maintenance() as connection:
            before = _logical_states(connection)
        self.migrate(stamp, 'f3b7c1d9e204')
        self.migrate(upgrade, 'head')
        self.migrate(upgrade, 'head')
        with self.db.managed.maintenance() as connection:
            self.assertEqual(_logical_states(connection), before)
        rows = self.db.query(SUBTITLEAUDITSTATE).order_by(SUBTITLEAUDITSTATE.ID).all()
        self.assertEqual([row.ID for row in rows], [41, 42])
        self.assertEqual([row.STATUS for row in rows], ['legacy', 'legacy'])
        validate_database(self.path)

    def test_missing_foreign_key_definition_cannot_be_stamped_as_current(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import inspect
        self.migrate(stamp, 'f3b7c1d9e204')
        self.migrate(upgrade, 'head')
        with self.db.managed.maintenance(foreign_keys=False) as connection:
            with connection.begin():
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                foreign = next(value for value in inspect(connection).get_foreign_keys('SUBTITLE_AUDIT_STATE')
                               if value['constrained_columns'] == ['PUBLICATION_ID'])
                name = foreign['name'] or 'fk_SUBTITLE_AUDIT_STATE_PUBLICATION_ID_SUBTITLE_PUBLICATION'
                # Preserve every column/unique key while removing only the FK.
                # integrity/FK row checks alone cannot reveal this definition gap.
                op = Operations(MigrationContext.configure(connection))
                with op.batch_alter_table('SUBTITLE_AUDIT_STATE', recreate='always',
                    naming_convention={'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}) as batch:
                    batch.drop_constraint(name, type_='foreignkey')
                self.assertFalse(_schema_matches(connection, versioned=True))

    def test_downgrade_materializes_only_latest_non_deleted_values(self):
        self.migrate(stamp, 'f3b7c1d9e204')
        self.migrate(upgrade, 'head')
        publish_now(self.db, audit_rows=[dict(SCOPE_KEY='scope', SERVER='emby',
            SUBTITLE_PATH='/media/0.mkv', STATUS='new', RESULT='{}', CONFIRMED_AT=2, UPDATED_AT=2)])
        with self.db.write_transaction():
            rows = self.db.query(SUBTITLEAUDITSTATE).filter_by(SUBTITLE_PATH='/media/1.mkv').all()
            delete_rows(self.db, SUBTITLEAUDITSTATE, rows)
        self.migrate(downgrade, 'f3b7c1d9e204')
        with self.db.managed.maintenance() as connection:
            rows = connection.exec_driver_sql('SELECT SUBTITLE_PATH,STATUS FROM SUBTITLE_AUDIT_STATE').all()
            self.assertEqual([tuple(row) for row in rows], [('/media/0.mkv', 'new')])
        validate_database(self.path)

    def test_failure_during_migration_rolls_back_schema_and_original_rows(self):
        self.migrate(stamp, 'f3b7c1d9e204')
        from unittest.mock import patch
        from app.db.models import SUBTITLETASK
        with self.db.managed.maintenance() as connection:
            before = _logical_states(connection)
        # The new index creation occurs after table rebuilds. Fail there and
        # assert SQLite really rolls the preceding DDL/data changes back.
        target = next(index for index in SUBTITLETASK.__table__.indexes
                      if index.name == 'INDX_SUBTITLE_TASK_INTERACTIVE_CLAIM')
        with patch.object(target, 'create', side_effect=RuntimeError('migration failure')):
            with self.assertRaises(RuntimeError):
                self.migrate(upgrade, 'head')
        with self.db.managed.maintenance() as connection:
            self.assertEqual(_logical_states(connection), before)
            columns = [row[1] for row in connection.exec_driver_sql('PRAGMA table_info(SUBTITLE_AUDIT_STATE)')]
            self.assertNotIn('PUBLICATION_ID', columns)
        validate_database(self.path)

    def test_process_death_during_ddl_recovers_hot_journal_before_validation(self):
        self.migrate(stamp, 'f3b7c1d9e204')
        with self.db.managed.maintenance() as connection:
            before = _logical_states(connection)
        self.db.close()
        code = '''
import os,sys
from pathlib import Path
from tests.test_database_governance import FileDatabase
from app.db.models import SUBTITLETASK
from alembic.config import Config
from alembic.command import upgrade
db=FileDatabase(sys.argv[1])
cfg=Config(); cfg.set_main_option('script_location',sys.argv[2])
index=next(value for value in SUBTITLETASK.__table__.indexes
           if value.name=='INDX_SUBTITLE_TASK_INTERACTIVE_CLAIM')
index.create=lambda *args,**kwargs: os._exit(29)
with db.managed.maintenance(foreign_keys=False) as connection:
    # Force dirty page spills before the exit hook so this tests SQLite's hot
    # rollback-journal recovery, rather than unflushed Python cache contents.
    connection.exec_driver_sql('PRAGMA cache_size=1')
    with connection.begin():
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        cfg.attributes['connection']=connection
        upgrade(cfg,'head')
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.path),
            str(Path(__file__).resolve().parents[1] / 'db_scripts')], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 29, result.stderr.decode())
        self.assertTrue(Path(str(self.path) + '-journal').exists())
        # The real bootstrap takes the instance lease and lets SQLite recover
        # before its readonly integrity/backup worker inspects the file.
        prepare(self.path.parent, DatabaseSettings(journal_mode='delete', reserve_free_mb=256))
        with self.db.managed.maintenance() as connection:
            self.assertEqual(_logical_states(connection), before)
            columns = [row[1] for row in connection.exec_driver_sql('PRAGMA table_info(SUBTITLE_AUDIT_STATE)')]
            self.assertNotIn('PUBLICATION_ID', columns)
        validate_database(self.path)
        self.migrate(upgrade, 'head')
        validate_database(self.path)
