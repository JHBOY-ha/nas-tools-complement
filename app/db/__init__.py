import os
import log
from config import Config
from .main_db import MainDb
from .main_db import DbPersist
from .media_db import MediaDb
from alembic.config import Config as AlembicConfig
from alembic.command import upgrade as alembic_upgrade, stamp as alembic_stamp
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, UniqueConstraint
import hashlib
import json

_HEAD = '7e1c9a42b605'


def init_db():
    """
    初始化数据库
    """
    log.console('开始初始化数据库...')
    from .runtime import prepare
    from .settings import DatabaseSettings
    directory = Config().get_config_path()
    prepare(directory, DatabaseSettings.from_config())
    # Back up before create_all changes even missing tables in an existing DB.
    from .main_db import _Database
    path = os.path.join(directory, 'user.db')
    if os.path.exists(path):
        with _Database.maintenance() as connection:
            if not _schema_matches(connection, versioned=True):
                from .backup import migration_backup
                from .runtime import _prepared
                state = _prepared[os.path.realpath(directory)]
                if not state['backup']:
                    state['backup'] = migration_backup(directory, _Database.settings)
    MediaDb().init_db()
    MainDb().init_db()
    update_db()
    log.console('数据库初始化完成')


def init_data():
    """
    初始化数据
    """
    log.console('开始初始化数据...')
    MainDb().init_data()
    log.console('数据初始化完成')


def update_db():
    """
    更新数据库
    """
    from .runtime import _prepared
    state = _prepared.get(os.path.realpath(Config().get_config_path()))
    if state is None:
        # Standalone callers need the same lease, restore checks and pre-upgrade
        # backup as application startup. init_db re-enters with prepared state.
        init_db()
        return
    if state and state['complete']:
        # init_db now completes controlled migration before callers can read
        # versioned tables. The legacy run.py follow-up is idempotent, without
        # a second full integrity scan or repeated schema stamping.
        return
    db_location = os.path.join(Config().get_config_path(), 'user.db')
    script_location = os.path.join(Config().get_root_path(), 'db_scripts')
    log.console('开始更新数据库...')
    from .main_db import _Database
    from .transactions import DatabaseWriteError
    from .runtime import complete, validate_database
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option('script_location', script_location)
    alembic_cfg.set_main_option('sqlalchemy.url', f"sqlite:///{db_location}")
    # Alembic receives this controlled connection instead of opening a second,
    # uncoordinated engine. SQLite batch rebuilds disable FKs only on this
    # offline connection, then integrity/FK checks gate the service startup.
    with _Database.maintenance(foreign_keys=False) as connection:
        with connection.begin():
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            alembic_cfg.attributes['connection'] = connection
            schema = inspect(connection)
            has_version = schema.has_table('alembic_version')
            if has_version:
                # Never stamp over an unknown future/application-specific head,
                # even when a subset of its columns happens to look familiar.
                scripts = ScriptDirectory.from_config(alembic_cfg)
                for revision in connection.exec_driver_sql('SELECT version_num FROM alembic_version'):
                    try:
                        scripts.get_revision(revision[0])
                    except Exception:
                        raise DatabaseWriteError('数据库迁移版本无法识别，升级已停止，请保留备份') from None
            if _schema_matches(connection, versioned=True):
                # No business tables will change. Keep revision validation and
                # integrity checks, but avoid two full payload scans per boot.
                alembic_stamp(alembic_cfg, _HEAD)
            else:
                before = _logical_states(connection)
                if not has_version:
                    if not _schema_matches(connection, versioned=False):
                        raise DatabaseWriteError('旧数据库 schema 无法安全识别，升级已停止，请保留备份')
                    alembic_stamp(alembic_cfg, 'f3b7c1d9e204')
                alembic_upgrade(alembic_cfg, 'head')
                if not _schema_matches(connection, versioned=True) or before != _logical_states(connection):
                    raise DatabaseWriteError('迁移后 schema 或逻辑数据核对失败，事务已回滚')
    validate_database(db_location)
    complete(Config().get_config_path())
    log.console('数据库更新完成')


def _schema_matches(connection, versioned):
    from .models import Base
    schema = inspect(connection)
    ignored = {'SUBTITLE_PUBLICATION', 'SUBTITLE_STATE_CLOCK', 'SUBTITLE_AUDIT_SCOPE_HEAD'}
    state_tables = {'SUBTITLE_AUDIT_STATE', 'SUBTITLE_MEDIA_STATUS'}
    required_indexes = {
        'SUBTITLE_TASK': {'INDX_SUBTITLE_TASK_INTERACTIVE_CLAIM': ('PRIORITY', 'CREATED_AT', 'ID'),
                          'INDX_SUBTITLE_TASK_AUDIT_CLAIM': ('CREATED_AT', 'ID')},
        'SUBTITLE_PROBE_CACHE': {'INDX_SUBTITLE_PROBE_CACHE_PATH': ('PATH',)},
        'SUBTITLE_AUDIT_STATE': {'INDX_SUBTITLE_AUDIT_STATE_PUBLICATION': ('PUBLICATION_ID', 'ID')},
        'SUBTITLE_MEDIA_STATUS': {'INDX_SUBTITLE_MEDIA_STATUS_PUBLICATION': ('PUBLICATION_ID', 'ID')},
        'SUBTITLE_PUBLICATION': {'INDX_SUBTITLE_PUBLICATION_STATE': ('STATUS', 'CREATED_AT')},
    }
    for name, table in Base.metadata.tables.items():
        if not versioned and name in ignored:
            continue
        if not schema.has_table(name):
            return False
        if versioned and name in required_indexes:
            actual_indexes = {value['name']: tuple(value['column_names']) for value in schema.get_indexes(name)}
            if any(actual_indexes.get(key) != columns for key, columns in required_indexes[name].items()):
                return False
        columns = {value['name']: value for value in schema.get_columns(name)}
        for column in table.columns:
            if not versioned and name in state_tables and column.name in ('PUBLICATION_ID', 'IS_DELETED'):
                continue
            if column.name not in columns:
                return False
            # An old integer OFFSET is not equivalent to the current Text
            # schema; do not stamp over unsupported legacy migrations.
            try:
                if columns[column.name]['type'].python_type != column.type.python_type:
                    return False
            except (AttributeError, NotImplementedError):
                return False
        if versioned and name in state_tables:
            expected = ('SCOPE_KEY', 'SERVER', 'SUBTITLE_PATH', 'PUBLICATION_ID') \
                if name == 'SUBTITLE_AUDIT_STATE' else ('SERVER', 'MEDIA_PATH', 'PUBLICATION_ID')
            if expected not in {tuple(value['column_names']) for value in schema.get_unique_constraints(name)}:
                return False
        if versioned and name in state_tables | ignored:
            # foreign_key_check cannot detect a constraint that was never
            # installed. Verify its definition before stamping current schema.
            actual_foreign = {(tuple(value['constrained_columns']), value['referred_table'],
                               tuple(value['referred_columns'])) for value in schema.get_foreign_keys(name)}
            for constraint in table.foreign_key_constraints:
                expected_foreign = (tuple(element.parent.name for element in constraint.elements),
                                    constraint.referred_table.name,
                                    tuple(element.column.name for element in constraint.elements))
                if expected_foreign not in actual_foreign:
                    return False
            actual_unique = {tuple(value['column_names']) for value in schema.get_unique_constraints(name)}
            for constraint in table.constraints:
                if isinstance(constraint, UniqueConstraint) and \
                        tuple(column.name for column in constraint.columns) not in actual_unique:
                    return False
    return True


def _logical_states(connection):
    """All existing business identities/payloads survive the additive migration."""
    from .models import Base
    result = {}
    schema = inspect(connection)
    metadata_tables = {'SUBTITLE_PUBLICATION', 'SUBTITLE_STATE_CLOCK', 'SUBTITLE_AUDIT_SCOPE_HEAD'}
    for table in sorted(set(Base.metadata.tables) - metadata_tables):
        if not schema.has_table(table):
            continue
        columns = [item['name'] for item in schema.get_columns(table)
                   if item['name'] not in ('PUBLICATION_ID', 'IS_DELETED')]
        digest = hashlib.sha256()
        keys = schema.get_pk_constraint(table)['constrained_columns'] or columns
        rows = connection.exec_driver_sql('SELECT %s FROM "%s" ORDER BY %s' % (
            ','.join('"%s"' % name for name in columns), table,
            ','.join('"%s"' % name for name in keys)))
        count = 0
        for row in rows:
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':'),
                                     default=repr).encode('utf-8'))
            digest.update(b'\n')
            count += 1
        result[table] = (count, digest.hexdigest())
    return result
