"""Bounded audit publication and query indexes; preserve legacy row identities.

Revision ID: 7e1c9a42b605
Revises: f3b7c1d9e204
"""
from alembic import op
import sqlalchemy as sa

revision = '7e1c9a42b605'
down_revision = 'f3b7c1d9e204'
branch_labels = None
depends_on = None


def upgrade():
    from app.db.models import (SUBTITLEPUBLICATION, SUBTITLESTATECLOCK,
                               SUBTITLEAUDITSCOPEHEAD, SUBTITLEAUDITSTATE,
                               SUBTITLEMEDIASTATUS, SUBTITLETASK, SUBTITLEPROBECACHE)
    from app.db.publication import seed_legacy
    bind = op.get_bind()
    for model in (SUBTITLEPUBLICATION, SUBTITLESTATECLOCK, SUBTITLEAUDITSCOPEHEAD):
        model.__table__.create(bind, checkfirst=True)
    seed_legacy(bind)
    for model, keys, old_name in (
            (SUBTITLEAUDITSTATE, ['SCOPE_KEY', 'SERVER', 'SUBTITLE_PATH'], 'UN_SUBTITLE_AUDIT_STATE_PATH'),
            (SUBTITLEMEDIASTATUS, ['SERVER', 'MEDIA_PATH'], 'UN_SUBTITLE_MEDIA_STATUS_PATH')):
        table = model.__tablename__
        columns = {column['name'] for column in sa.inspect(bind).get_columns(table)}
        if 'PUBLICATION_ID' not in columns:
            uniques = {item['name'] for item in sa.inspect(bind).get_unique_constraints(table)}
            with op.batch_alter_table(table, recreate='always') as batch:
                if old_name in uniques:
                    batch.drop_constraint(old_name, type_='unique')
                batch.add_column(sa.Column('PUBLICATION_ID', sa.Text(), nullable=False,
                                           server_default=sa.text("'legacy'")))
                batch.add_column(sa.Column('IS_DELETED', sa.Integer(), nullable=False,
                                           server_default=sa.text('0')))
                batch.create_foreign_key('FK_' + table + '_PUBLICATION',
                                          'SUBTITLE_PUBLICATION', ['PUBLICATION_ID'], ['ID'])
                batch.create_unique_constraint('UN_' + table + '_VERSION', keys + ['PUBLICATION_ID'])
    # Keep the original six indexes and add exact queue ordering/explicit PATH.
    for model in (SUBTITLEAUDITSTATE, SUBTITLEMEDIASTATUS, SUBTITLETASK, SUBTITLEPROBECACHE):
        for index in model.__table__.indexes:
            index.create(bind, checkfirst=True)


def downgrade():
    from app.db.models import SUBTITLEAUDITSTATE, SUBTITLEMEDIASTATUS
    from app.db.publication import latest_criterion
    bind = op.get_bind()
    # Materialize only currently visible data. Code rollback alone would lose
    # replacement/tombstone semantics and could expose superseded values.
    for model, keys, original in (
            (SUBTITLEAUDITSTATE, ['SCOPE_KEY', 'SERVER', 'SUBTITLE_PATH'], 'UN_SUBTITLE_AUDIT_STATE_PATH'),
            (SUBTITLEMEDIASTATUS, ['SERVER', 'MEDIA_PATH'], 'UN_SUBTITLE_MEDIA_STATUS_PATH')):
        table = model.__tablename__
        bind.execute(model.__table__.delete().where(~latest_criterion(model)))
        op.drop_index('INDX_' + table + '_PUBLICATION', table_name=table)
        # SQLAlchemy 1.4's SQLite reflector can return None for a quoted FK
        # name. Name reflected constraints deterministically before removing.
        names = {'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}
        foreign = next(value for value in sa.inspect(bind).get_foreign_keys(table)
                       if value['constrained_columns'] == ['PUBLICATION_ID'])
        foreign_name = foreign['name'] or ('fk_' + table + '_PUBLICATION_ID_SUBTITLE_PUBLICATION')
        with op.batch_alter_table(table, recreate='always', naming_convention=names) as batch:
            batch.drop_constraint(foreign_name, type_='foreignkey')
            batch.drop_constraint('UN_' + table + '_VERSION', type_='unique')
            batch.drop_column('PUBLICATION_ID')
            batch.drop_column('IS_DELETED')
            batch.create_unique_constraint(original, keys)
    op.drop_table('SUBTITLE_AUDIT_SCOPE_HEAD')
    op.drop_table('SUBTITLE_STATE_CLOCK')
    op.drop_table('SUBTITLE_PUBLICATION')
    op.drop_index('INDX_SUBTITLE_TASK_INTERACTIVE_CLAIM', table_name='SUBTITLE_TASK')
    op.drop_index('INDX_SUBTITLE_TASK_AUDIT_CLAIM', table_name='SUBTITLE_TASK')
    op.drop_index('INDX_SUBTITLE_PROBE_CACHE_PATH', table_name='SUBTITLE_PROBE_CACHE')
