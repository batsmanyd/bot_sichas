"""Add transactional notification outbox."""

from alembic import op
import sqlalchemy as sa

revision = "0004_notification_outbox"
down_revision = "0003_moderation_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("dedupe_key", sa.String(120), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(180), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("kind", "dedupe_key", "status", "next_attempt_at"):
        op.create_index(f"ix_notification_outbox_{column}", "notification_outbox", [column])


def downgrade():
    op.drop_table("notification_outbox")
