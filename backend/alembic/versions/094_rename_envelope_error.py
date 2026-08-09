"""counts_as_false_positive -> counts_as_envelope_error

The old name states a quantity this system cannot measure and does not need.

A FALSE-POSITIVE RATE is P(auto-post | the match is actually wrong). Its denominator
includes rows the envelope did NOT select, which are never verified — the term is
structurally unidentifiable, and no estimator recovers it.

What the autonomy decision actually needs is the other conditional: of the rows we
WOULD post unattended, what fraction are wrong. That is 1 - precision, the false
DISCOVERY rate, and it is estimable from a sample drawn entirely inside the envelope.
The two differ by roughly 50x in worked examples; shipping one under the other's name
is how a 0.4% becomes a 20% on somebody's slide.

Renamed rather than deprecated because the column has NO production rows: as of
2026-08-09 no reject has ever been recorded on any tenant (348,909 reconciliation
results, zero dispositions of any kind). There is no historical payload to preserve
and no dual-read window to honour, so the honest move is to make the name true now,
while it is free.

ALTER TABLE ... RENAME COLUMN is a catalog-only change: no table rewrite, no lock
held for the length of a scan. Safe on the largest table in this schema.

Revision ID: 094_rename_envelope_error
Revises: 093_recon_reject_labels
Create Date: 2026-08-09

⚠️ Parented to main's current head. `feat/rolling-period` carries 093_report_series +
094_dashboard_preference_series, also children of 092/093 — whichever branch merges
SECOND must re-parent (linearize), never add a merge migration, which breaks
`downgrade -1`. This branch has already done that favour once in the other direction.
"""

from alembic import op

revision = "094_rename_envelope_error"
down_revision = "093_recon_reject_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "reconciliation_results",
        "counts_as_false_positive",
        new_column_name="counts_as_envelope_error",
    )


def downgrade() -> None:
    op.alter_column(
        "reconciliation_results",
        "counts_as_envelope_error",
        new_column_name="counts_as_false_positive",
    )
