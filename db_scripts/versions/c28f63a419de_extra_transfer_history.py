"""Track local extras without counting them as movies or episodes."""
from alembic import op
import sqlalchemy as sa

revision = 'c28f63a419de'
down_revision = 'ab912e4f6c20'
branch_labels = None
depends_on = None


def upgrade():
    # Startup create_all and repeated upgrades may have already created the table.
    if not sa.inspect(op.get_bind()).has_table('EXTRA_TRANSFER_HISTORY'):
        op.create_table('EXTRA_TRANSFER_HISTORY',
                        sa.Column('DEST_PATH', sa.Text, primary_key=True),
                        sa.Column('SOURCE_PATH', sa.Text, nullable=False),
                        sa.Column('PARENT_TYPE', sa.Text, nullable=False),
                        sa.Column('PARENT_ID', sa.Integer, nullable=False),
                        sa.Column('CATEGORY', sa.Text, nullable=False),
                        sa.Column('MODE', sa.Text, nullable=False),
                        sa.Column('COMPLETED_AT', sa.Text, nullable=False))


def downgrade():
    if sa.inspect(op.get_bind()).has_table('EXTRA_TRANSFER_HISTORY'):
        op.drop_table('EXTRA_TRANSFER_HISTORY')
