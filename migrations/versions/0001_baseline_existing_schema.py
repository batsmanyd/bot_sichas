"""Baseline the schema present at application version 0.17.8.

Existing environments must be backed up and stamped with this revision before
applying later migrations. The baseline intentionally performs no DDL.
"""

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
