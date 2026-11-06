"""speaker diarization segments

Revision ID: 005
Revises: 004
Create Date: 2026-10-06 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("segments", sa.JSON, nullable=True))
    op.add_column("meetings", sa.Column("speaker_names", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "speaker_names")
    op.drop_column("meetings", "segments")
