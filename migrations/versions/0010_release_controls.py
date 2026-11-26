"""Store commercial terms, private material, audit events and release controls."""

from alembic import op
import sqlalchemy as sa

revision = "0010_release_controls"
down_revision = "0009_private_features"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_details",
        sa.Column("task_id", sa.Integer, sa.ForeignKey("private_task.id"), primary_key=True),
        sa.Column("city", sa.String(100)), sa.Column("country", sa.String(2)),
        sa.Column("organization", sa.String(120)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("requirements", sa.String(500), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fee_bps", sa.Integer, nullable=False),
    )
    op.create_table(
        "task_material",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("private_task.id"), nullable=False),
        sa.Column("image", sa.Text, nullable=False),
    )
    op.create_table(
        "task_activity",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("private_task.id"), nullable=False),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in ("task_material", "task_activity"):
        op.create_index(f"ix_{table}_task_id", table, ["task_id"])
    op.create_table(
        "release_setting",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade():
    for table in ("release_setting", "task_activity", "task_material", "task_details"):
        op.drop_table(table)
