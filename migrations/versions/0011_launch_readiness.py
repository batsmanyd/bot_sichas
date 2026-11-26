"""Add verification, opt-in work areas and shared operational counters."""

from alembic import op
import sqlalchemy as sa

revision = "0011_launch_readiness"
down_revision = "0010_release_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("verified_organizer",
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
                    sa.Column("name", sa.String(120), nullable=False),
                    sa.Column("active", sa.Boolean, nullable=False))
    op.create_table("organizer_coordinator",
                    sa.Column("organizer_id", sa.Integer, sa.ForeignKey("verified_organizer.user_id"), primary_key=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
                    sa.Column("label", sa.String(120), nullable=False))
    op.create_table("deed_coordination",
                    sa.Column("deed_id", sa.Integer, sa.ForeignKey("good_deed_v2.id"), primary_key=True),
                    sa.Column("coordinator_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False))
    op.create_table("task_work_area",
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
                    sa.Column("country", sa.String(2), nullable=False),
                    sa.Column("city", sa.String(100), nullable=False))
    op.create_index("ix_task_work_area_country", "task_work_area", ["country"])
    op.create_table("public_rate_bucket", sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("window", sa.Integer, nullable=False), sa.Column("count", sa.Integer, nullable=False))
    op.create_index("ix_public_rate_bucket_window", "public_rate_bucket", ["window"])
    op.create_table("daily_activity",
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
                    sa.Column("day", sa.String(10), primary_key=True))
    op.create_index("ix_daily_activity_day", "daily_activity", ["day"])


def downgrade():
    for name in ("daily_activity", "public_rate_bucket", "task_work_area",
                 "deed_coordination", "organizer_coordinator", "verified_organizer"):
        op.drop_table(name)
