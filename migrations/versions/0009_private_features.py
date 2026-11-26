"""Add opt-in Telegram settings, company watches, acquisition and private tasks."""
from alembic import op
import sqlalchemy as sa

revision = "0009_private_features"
down_revision = "0008_safety_good_deeds"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_preference",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("enabled", sa.Integer, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
    )
    op.create_table(
        "company_watch",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("radius", sa.Integer, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matched", sa.Integer, nullable=False),
    )
    op.create_table(
        "acquisition",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("campaign", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "private_task",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False),
        sa.Column("assignee_id", sa.Integer, sa.ForeignKey("user.id")),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("budget", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", sa.String(500)),
        sa.Column("dispute", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("owner_id", "assignee_id", "status"):
        op.create_index(f"ix_private_task_{column}", "private_task", [column])
    op.create_table(
        "task_application",
        sa.Column("task_id", sa.Integer, sa.ForeignKey("private_task.id"), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
    )


def downgrade():
    op.execute("DELETE FROM notification_outbox WHERE kind = 'user_telegram'")
    for table in ("task_application", "private_task", "acquisition", "company_watch", "telegram_preference"):
        op.drop_table(table)
