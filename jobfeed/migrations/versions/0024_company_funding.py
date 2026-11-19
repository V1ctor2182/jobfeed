"""Keep the last funding check of each company the recruiter sheet asked about."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_funding",
        sa.Column("company_key", sa.Text(), primary_key=True),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("found", sa.Integer(), nullable=False),
        sa.Column("round", sa.Text()),
        sa.Column("amount_usd", sa.Float()),
        sa.Column("announced_on", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("checked_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("company_funding")
