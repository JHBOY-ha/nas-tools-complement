import os
import re
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
    # Back up before create_all changes a structural schema.  Ordinary query
    # indexes are deliberately checked/repaired after migration and do not
    # turn an otherwise current database into a migration backup.
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
            # A current Alembic head can still have lost a model-owned query
            # index. Repair every explicit Base Index in this already-controlled
            # maintenance unit; this path intentionally does not calculate
            # business digests.
            _ensure_query_indexes(connection)
            # _ensure_query_indexes validates the complete index contract. Keep
            # the final pass structural so a repaired index is not reflected a
            # second time during the same startup transaction.
            if not _schema_matches(connection, versioned=True):
                raise DatabaseWriteError('数据库 schema、查询索引或完整性约束核对失败，启动已停止')
    validate_database(db_location)
    complete(Config().get_config_path())
    log.console('数据库更新完成')


def _schema_matches(connection, versioned):
    """Check tables, columns and integrity constraints, not query indexes."""
    from .models import Base
    schema = inspect(connection)
    ignored = {'SUBTITLE_PUBLICATION', 'SUBTITLE_STATE_CLOCK', 'SUBTITLE_AUDIT_SCOPE_HEAD'}
    state_tables = {'SUBTITLE_AUDIT_STATE', 'SUBTITLE_MEDIA_STATUS'}
    for name, table in Base.metadata.tables.items():
        if not versioned and name in ignored:
            continue
        if not schema.has_table(name):
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


def _query_index_definitions():
    """Return every explicit main-database model index in stable order."""
    from .models import Base
    return tuple(sorted(
        (index for table in Base.metadata.tables.values()
         for index in table.indexes),
        key=lambda index: (index.table.name, index.name or '')
    ))


def _index_columns(index):
    """Normalize model index expressions to the reflected column order."""
    columns = []
    for expression in index.expressions:
        name = getattr(expression, 'name', None)
        if not name:
            # The only expression currently used by the model is
            # ``PRIORITY DESC``. Keep the expression's leading column name so
            # SQLAlchemy's model and SQLite's inspector compare the same order.
            name = str(expression).strip().split()[0].strip('"`[]')
        columns.append(name)
    return tuple(columns)


def _index_directions(index):
    """Return SQLite's 0/1 ascending/descending flag for each model column."""
    directions = []
    for expression in index.expressions:
        tokens = str(expression).strip().split()
        directions.append(1 if tokens and tokens[-1].upper() == 'DESC' else 0)
    return tuple(directions)


def _normalize_sql_fragment(value):
    """Normalize SQL outside quoted values without changing literal meaning."""
    if value is None:
        return None
    source = str(value).strip()
    if source.endswith(';'):
        source = source[:-1].rstrip()
    parts = []
    outside = []
    index = 0
    while index < len(source):
        character = source[index]
        if character in ("'", '"', '`') or character == '[':
            if outside:
                parts.append(re.sub(r'\s+', ' ', ''.join(outside)).upper())
                outside = []
            closing = ']' if character == '[' else character
            start = index
            index += 1
            while index < len(source):
                if source[index] == closing:
                    # SQL escapes quote characters by doubling them. Preserve
                    # the complete quoted token so string case stays exact.
                    if index + 1 < len(source) and source[index + 1] == closing:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            parts.append(source[start:index])
            continue
        outside.append(character)
        index += 1
    if outside:
        parts.append(re.sub(r'\s+', ' ', ''.join(outside)).upper())
    return ''.join(parts).strip()


def _index_where_from_sql(sql):
    """Extract a partial-index predicate from one sqlite_master definition."""
    if not sql:
        return None
    match = re.search(r'\bWHERE\b(.+)$', str(sql), flags=re.IGNORECASE | re.DOTALL)
    return _normalize_sql_fragment(match.group(1)) if match else None


def _pragma_identifier(identifier):
    """Quote a model-owned identifier for SQLite PRAGMA statements."""
    return '"%s"' % str(identifier).replace('"', '""')


def _index_actual_directions(connection, index_name):
    """Read explicit ASC/DESC flags; Inspector.get_indexes omits them."""
    rows = connection.exec_driver_sql(
        'PRAGMA index_xinfo(%s)' % _pragma_identifier(index_name)
    )
    # index_xinfo columns are seqno, cid, name, desc, coll, key.  The implicit
    # rowid entry has key=0 and is not part of the model index definition.
    return tuple(int(row[3]) for row in rows if row[5])


def _query_index_reflection(connection, indexes):
    """Collect one reflection snapshot for all model indexes on a connection."""
    schema = inspect(connection)
    table_names = sorted({index.table.name for index in indexes})
    actual_by_table = {}
    partial_by_name = {}
    for table_name in table_names:
        if not schema.has_table(table_name):
            actual_by_table[table_name] = {}
            continue
        actual_by_table[table_name] = {
            value['name']: value for value in schema.get_indexes(table_name)
            if value.get('name')
        }
        rows = connection.exec_driver_sql(
            'PRAGMA index_list(%s)' % _pragma_identifier(table_name)
        )
        for row in rows:
            if len(row) < 2:
                continue
            # SQLite 3.8+ exposes partial as column 4. Older runtimes cannot
            # create the partial indexes in this model, so False is conservative
            # for ordinary indexes and a mismatch for expected partial ones.
            partial_by_name[row[1]] = bool(row[4]) if len(row) > 4 else False
    sql_by_name = {
        row[0]: row[1] for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='index'")
    }
    expected_names = {index.name for index in indexes if index.name}
    directions_by_name = {}
    for actual_indexes in actual_by_table.values():
        for name in actual_indexes:
            if name in expected_names and name not in directions_by_name:
                directions_by_name[name] = _index_actual_directions(connection, name)
    return {
        'indexes': actual_by_table,
        'partial': partial_by_name,
        'sql': sql_by_name,
        'directions': directions_by_name,
    }


def _query_index_mismatches(index, actual, reflection):
    """Return definition differences while keeping SQL literals case-sensitive."""
    expected_columns = _index_columns(index)
    actual_columns = tuple(actual.get('column_names') or ())
    expected_directions = _index_directions(index)
    actual_directions = reflection['directions'].get(index.name, ())
    expected_where = _normalize_sql_fragment(index.dialect_options['sqlite'].get('where'))
    actual_where = _index_where_from_sql(reflection['sql'].get(index.name))
    expected_partial = expected_where is not None
    actual_partial = bool(reflection['partial'].get(index.name, False))
    mismatches = []
    if actual_columns != expected_columns:
        mismatches.append('列顺序应为 (%s)，实际为 (%s)' % (
            ', '.join(expected_columns), ', '.join(actual_columns) or '<无列>'))
    if actual_directions != expected_directions:
        mismatches.append('排序方向应为 %s，实际为 %s' %
                          (expected_directions, actual_directions))
    if bool(actual.get('unique')) != bool(index.unique):
        mismatches.append('unique 应为 %s，实际为 %s' %
                          (bool(index.unique), bool(actual.get('unique'))))
    if actual_partial != expected_partial:
        mismatches.append('partial 应为 %s，实际为 %s' %
                          (expected_partial, actual_partial))
    if actual_where != expected_where:
        mismatches.append('WHERE 谓词不匹配，期望 %r，实际为 %r' %
                          (expected_where, actual_where))
    return mismatches


def _query_index_state(connection, indexes=None):
    """Reflect once, validate all definitions, and return only missing indexes."""
    from .transactions import DatabaseWriteError
    indexes = _query_index_definitions() if indexes is None else tuple(indexes)
    reflection = _query_index_reflection(connection, indexes)
    missing = []
    for index in indexes:
        actual = reflection['indexes'].get(index.table.name, {}).get(index.name)
        if actual is None:
            missing.append(index)
            continue
        mismatches = _query_index_mismatches(index, actual, reflection)
        if mismatches:
            raise DatabaseWriteError(
                '查询索引定义不匹配：%s.%s；%s' %
                (index.table.name, index.name, '；'.join(mismatches))
            )
    return missing


def _query_indexes_match(connection):
    """Check the model-owned query-index contract without changing the DB."""
    return not _query_index_state(connection)


def _ensure_query_indexes(connection):
    """Create only missing model indexes after validating all existing ones."""
    from .transactions import DatabaseWriteError
    missing = _query_index_state(connection)
    for index in missing:
        try:
            # checkfirst=False is intentional: the reflected same-name case
            # was validated above, so CREATE must not silently accept a wrong
            # definition through IF NOT EXISTS.
            index.create(bind=connection, checkfirst=False)
        except Exception as error:
            # Keep the sanitized startup error actionable without exposing SQL
            # parameters. DBAPI errors carry the useful disk/duplicate reason.
            detail = getattr(error, 'orig', None) or error
            detail = re.sub(r'\s+', ' ', str(detail).strip())
            raise DatabaseWriteError(
                '查询索引补建失败：%s.%s；原因：%s；请检查磁盘空间、数据库权限及同名索引' %
                (index.table.name, index.name, detail or type(error).__name__)
            ) from None
    return len(missing)


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
