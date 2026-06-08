"""Reconcile the two parallel 080 heads into one.

feat/metric-definition-catalog added the metric-catalog line
``080_metric_definitions -> 081_metric_definitions_hardening -> 082_metric_def_with_check``
while main independently added ``080_learned_rules_rls`` — both branching off
``079_order_ref_pattern``. With both lines present, ``alembic upgrade head`` fails
("Multiple head revisions are present").

This is a pure merge migration: it joins the two heads into a single head so the
revision graph is linear-from-head again. It makes NO schema changes — the two
branches touch disjoint tables (metric_definitions vs learned_rules) and never
conflict, so nothing needs reconciling beyond the revision pointers themselves.
"""

revision = "083_merge_metric_learned_rls"
down_revision = ("080_learned_rules_rls", "082_metric_def_with_check")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: this revision only reconciles two parallel branches."""


def downgrade() -> None:
    """No-op: reverting re-exposes the two pre-existing heads, no DDL needed."""
