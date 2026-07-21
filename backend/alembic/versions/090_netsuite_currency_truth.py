"""netsuite_currency_truth — Phase A of the recon washout/currency-truth plan.

``netsuite_postings.amount``/``currency`` mislabeled foreign deposits: the sync
selected ``t.total`` (subsidiary BASE-currency amount) but labeled it with
``BUILTIN.DF(t.currency)`` (TRANSACTION currency). This adds the columns the
fixed sync needs to record the truth honestly instead of overloading
``currency``. Additive; no backfill (values unknowable offline — the sync
re-populates on next run).
"""

import sqlalchemy as sa

from alembic import op

revision = "090_netsuite_currency_truth"
down_revision = "089_recon_resolution_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("netsuite_postings", sa.Column("transaction_currency", sa.String(3), nullable=True))
    op.add_column("netsuite_postings", sa.Column("foreign_amount", sa.Numeric(15, 2), nullable=True))
    op.add_column("netsuite_postings", sa.Column("exchange_rate", sa.Numeric(12, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("netsuite_postings", "exchange_rate")
    op.drop_column("netsuite_postings", "foreign_amount")
    op.drop_column("netsuite_postings", "transaction_currency")
