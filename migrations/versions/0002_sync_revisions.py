"""Add durable synchronization revisions."""

from alembic import op
import sqlalchemy as sa

revision = "0002_sync_revisions"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sync_revision",
        sa.Column("key", sa.String(length=120), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("sync_revision")
