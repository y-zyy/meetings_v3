"""admin correction rules table

Revision ID: 004
Revises: 003
Create Date: 2026-06-26 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_correction_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wrong", sa.String(255), nullable=False),
        sa.Column("correct", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table("admin_correction_rules")
