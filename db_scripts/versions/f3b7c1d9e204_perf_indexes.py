"""Add indexes for subtitle task history, probe cache and transfer history.

These queries previously fell back to a full table scan plus sort as the
history tables grew, and each scan ran while the subtitle task manager held
its global lock.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3b7c1d9e204'
down_revision = 'c28f63a419de'
branch_labels = None
depends_on = None


# (index name, table, column list). CREATE INDEX IF NOT EXISTS keeps this
# idempotent: fresh databases already get these from create_all. Current
# startup validation owns the complete model-index contract after migrations.
_INDEXES = [
    ('INDX_SUBTITLE_TASK_CREATED', 'SUBTITLE_TASK', 'CREATED_AT'),
    ('INDX_SUBTITLE_TASK_FINISHED', 'SUBTITLE_TASK', 'FINISHED_AT'),
    ('INDX_SUBTITLE_TASK_QUEUE', 'SUBTITLE_TASK', 'TYPE, STATUS, PRIORITY, CREATED_AT'),
    ('INDX_SUBTITLE_PROBE_CACHE_PAIR', 'SUBTITLE_PROBE_CACHE', 'PAIR_PATH'),
    ('INDX_SUBTITLE_AUDIT_STATE_PATH', 'SUBTITLE_AUDIT_STATE', 'SUBTITLE_PATH'),
    ('INDX_TRANSFER_HISTORY_DATE', 'TRANSFER_HISTORY', 'DATE'),
]


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for name, table, columns in _INDEXES:
        if table not in existing_tables:
            continue
        op.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS %s ON %s (%s)" % (name, table, columns)
        ))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for name, table, _columns in _INDEXES:
        if table not in existing_tables:
            continue
        op.execute(sa.text("DROP INDEX IF EXISTS %s" % name))
