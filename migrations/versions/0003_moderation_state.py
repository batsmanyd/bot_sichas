"""Add persistent complaint state and moderation audit trail."""

from alembic import op
import sqlalchemy as sa

revision = "0003_moderation_state"
down_revision = "0002_sync_revisions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_report") as batch:
        batch.add_column(sa.Column("category", sa.String(40), nullable=False, server_default="other"))
        batch.add_column(sa.Column("status", sa.String(30), nullable=False, server_default="new"))
        batch.add_column(sa.Column("moderator_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("decision", sa.String(40), nullable=True))
        batch.add_column(sa.Column("decision_reason", sa.String(180), nullable=True))
        batch.add_column(sa.Column("auto_hidden", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_report_moderator", "user", ["moderator_id"], ["id"])
        batch.create_index("ix_user_report_category", ["category"])
        batch.create_index("ix_user_report_status", ["status"])
        batch.create_index("ix_user_report_moderator_id", ["moderator_id"])
    op.create_table(
        "moderation_action",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("user_report.id"), nullable=True),
        sa.Column("moderator_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(180), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_moderation_action_report_id", "moderation_action", ["report_id"])
    op.create_index("ix_moderation_action_moderator_id", "moderation_action", ["moderator_id"])
    op.create_index("ix_moderation_action_action", "moderation_action", ["action"])
    op.create_index("ix_moderation_action_created_at", "moderation_action", ["created_at"])


def downgrade():
    op.drop_table("moderation_action")
    with op.batch_alter_table("user_report") as batch:
        batch.drop_index("ix_user_report_moderator_id")
        batch.drop_index("ix_user_report_status")
        batch.drop_index("ix_user_report_category")
        batch.drop_constraint("fk_report_moderator", type_="foreignkey")
        for name in ("reviewed_at", "auto_hidden", "decision_reason", "decision", "moderator_id", "status", "category"):
            batch.drop_column(name)
