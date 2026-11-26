"""Add indexes used by moderation filters and runtime schema guard."""

from alembic import op

revision = "0007_runtime_guards"
down_revision = "0006_integrity_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_user_report_decision", "user_report", ["decision"])
    op.create_index("ix_user_moderation_hidden_until", "user_moderation", ["hidden_until"])


def downgrade():
    op.drop_index("ix_user_moderation_hidden_until", table_name="user_moderation")
    op.drop_index("ix_user_report_decision", table_name="user_report")
