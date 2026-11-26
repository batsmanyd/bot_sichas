"""Store separate consent for task matches and task events."""

from alembic import op
import sqlalchemy as sa

revision = "0012_task_alerts"
down_revision = "0011_launch_readiness"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_alert_preference",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("matching", sa.Boolean, nullable=False),
        sa.Column("events", sa.Boolean, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
    )


def downgrade():
    op.drop_table("task_alert_preference")
