"""Keep what the user does with a recruiter sheet row, keyed by real job."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recruiter_sheet_rows",
        sa.Column(
            "real_job_id",
            sa.BigInteger(),
            sa.ForeignKey("real_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("recruiter", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("recruiter_sheet_rows")
