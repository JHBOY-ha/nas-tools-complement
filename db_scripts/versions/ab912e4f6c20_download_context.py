"""Persist the recognized identity of download tasks.

Revision ID: ab912e4f6c20
Revises: 720a6289a697
"""
from alembic import op
import sqlalchemy as sa

revision = 'ab912e4f6c20'
down_revision = '720a6289a697'
branch_labels = None
depends_on = None


def upgrade():
    # Startup also runs create_all, so both upgrade paths must be idempotent.
    if not sa.inspect(op.get_bind()).has_table('DOWNLOAD_CONTEXT'):
        op.create_table('DOWNLOAD_CONTEXT',
                        sa.Column('ID', sa.Text, primary_key=True),
                        sa.Column('DOWNLOADER', sa.Text),
                        sa.Column('PAYLOAD', sa.Text))


def downgrade():
    if sa.inspect(op.get_bind()).has_table('DOWNLOAD_CONTEXT'):
        op.drop_table('DOWNLOAD_CONTEXT')
