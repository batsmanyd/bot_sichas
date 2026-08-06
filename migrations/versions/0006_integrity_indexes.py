"""Add indexes used by active-meeting and complaint checks."""

from alembic import op

revision = "0006_integrity_indexes"
down_revision = "0005_operation_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_interest_user_status_meeting", "interest", ["user_id", "status", "meeting_id"])
    op.create_index("ix_interest_meeting_status", "interest", ["meeting_id", "status"])
    op.create_index("ix_meeting_owner_expires", "meeting", ["owner_id", "expires_at"])
    op.create_index("ix_report_target_reporter_created", "user_report", ["target_id", "reporter_id", "created_at"])


def downgrade():
    op.drop_index("ix_report_target_reporter_created", table_name="user_report")
    op.drop_index("ix_meeting_owner_expires", table_name="meeting")
    op.drop_index("ix_interest_meeting_status", table_name="interest")
    op.drop_index("ix_interest_user_status_meeting", table_name="interest")
