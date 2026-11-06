"""system_settings table

Revision ID: 002
Revises: 001
Create Date: 2026-01-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(1024), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
