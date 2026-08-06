"""Add mutation operation idempotency records."""

from alembic import op
import sqlalchemy as sa

revision = "0005_operation_idempotency"
down_revision = "0004_notification_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operation_record",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("operation_id", sa.String(80), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(180), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "operation_id", name="uq_operation_user_id"),
    )
    op.create_index("ix_operation_record_user_id", "operation_record", ["user_id"])
    op.create_index("ix_operation_record_created_at", "operation_record", ["created_at"])


def downgrade():
    op.drop_table("operation_record")
