"""initial schema

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("full_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("meeting_date", sa.Date, nullable=True),
        sa.Column("location", sa.String(255), nullable=False, server_default=""),
        sa.Column("attendees", sa.Text, nullable=False, server_default=""),
        sa.Column("agenda", sa.Text, nullable=False, server_default=""),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("language", sa.String(8), nullable=False, server_default="ko"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("file_path", sa.String(512), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("file_size", sa.Integer, nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("transcript", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_meetings_created_by", "meetings", ["created_by"])
    op.create_index("ix_meetings_status", "meetings", ["status"])
    op.create_index("ix_meetings_created_at", "meetings", ["created_at"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("meeting_id", sa.Integer, sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("order", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_decisions_meeting_id", "decisions", ["meeting_id"])

    op.create_table(
        "action_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("meeting_id", sa.Integer, sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("assignee", sa.String(128), nullable=True),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("order", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_action_items_meeting_id", "action_items", ["meeting_id"])


def downgrade() -> None:
    op.drop_table("action_items")
    op.drop_table("decisions")
    op.drop_table("meetings")
    op.drop_table("users")
