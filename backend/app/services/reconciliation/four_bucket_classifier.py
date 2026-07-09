"""Pure four-bucket classifier for the recon reviewer (R1 + R2a materiality).

Maps each ReconciliationResult to exactly one bucket from match_type + variance
presence, with optional materiality routing (R2a): a matched line whose variance
is *material* (over an absolute OR relative threshold) is routed to needs_review
instead of auto_classifications/rules. When both thresholds are ``None`` the
classifier is byte-identical to R1 (back-compat). NO confidence threshold drives
buckets, NO collision detection, NO matching/engine behavior change. Stdlib-only
so schemas may import it safely; the SQL twin filters the persisted ``bucket``
column.
"""

from __future__ import annotations

from decimal import Decimal

BUCKET_MATCHES = "matches"
BUCKET_RULES = "rules"
BUCKET_AUTO_CLASSIFICATIONS = "auto_classifications"
BUCKET_NEEDS_REVIEW = "needs_review"

ALL_BUCKETS = (BUCKET_MATCHES, BUCKET_RULES, BUCKET_AUTO_CLASSIFICATIONS, BUCKET_NEEDS_REVIEW)
BULK_APPROVABLE_BUCKETS = (BUCKET_MATCHES, BUCKET_RULES, BUCKET_AUTO_CLASSIFICATIONS)

# Rows already dispositioned can never be acted on again. Canonical home of the
# invariant — shared by the bulk-approve guard (API) and the autonomy envelope.
# carried_forward: an acknowledged reconciling item — bulk-approve must not flip it.
TERMINAL_RESULT_STATUSES = ("approved", "rejected", "locked", "carried_forward")


def _has_variance(variance_type: str | None, variance_amount: Decimal | None) -> bool:
    if variance_type is not None:
        return True
    return variance_amount is not None and variance_amount != 0


def _is_material(
    variance_amount: Decimal | None,
    matched_amount: Decimal | None,
    materiality_abs: Decimal | None,
    materiality_pct: Decimal | None,
) -> bool:
    """True iff the variance breaches the absolute OR relative materiality threshold.

    Decimal-safe throughout (never float). When BOTH thresholds are ``None`` this
    returns ``False`` unconditionally, which preserves R1 behavior exactly.
    """
    if variance_amount is None:
        return False
    abs_variance = abs(variance_amount)
    if materiality_abs is not None and abs_variance > materiality_abs:
        return True
    if (
        materiality_pct is not None
        and matched_amount is not None
        and matched_amount != 0
        and abs_variance / abs(matched_amount) > materiality_pct
    ):
        return True
    return False


# Public alias — the ResolutionPlanner computes proposal-level materiality with
# the exact same predicate the bucket router uses (single source of truth).
def is_material(
    variance_amount: Decimal | None,
    matched_amount: Decimal | None,
    materiality_abs: Decimal | None,
    materiality_pct: Decimal | None,
) -> bool:
    return _is_material(variance_amount, matched_amount, materiality_abs, materiality_pct)


def classify(
    match_type: str,
    variance_type: str | None,
    variance_amount: Decimal | None,
    *,
    materiality_abs: Decimal | None = None,
    materiality_pct: Decimal | None = None,
    matched_amount: Decimal | None = None,
) -> str:
    """Return the bucket id for one result's fields. Total + deterministic.

    R2a waterfall (decimal-safe):
      - deterministic + no variance → matches
      - (deterministic | fuzzy) + has variance + material → needs_review
      - deterministic + has variance → auto_classifications
      - fuzzy → rules
      - else (unmatched / exception / unknown) → needs_review

    When ``materiality_abs`` and ``materiality_pct`` are both ``None`` the material
    branch can never fire, so this reduces to the R1 matrix exactly.
    """
    has_variance = _has_variance(variance_type, variance_amount)

    if match_type == "deterministic" and not has_variance:
        return BUCKET_MATCHES

    if (
        match_type in ("deterministic", "fuzzy")
        and has_variance
        and _is_material(variance_amount, matched_amount, materiality_abs, materiality_pct)
    ):
        return BUCKET_NEEDS_REVIEW

    if match_type == "deterministic":
        return BUCKET_AUTO_CLASSIFICATIONS
    if match_type == "fuzzy":
        return BUCKET_RULES
    # unmatched, exception (payout dup), or any unknown future type → safe default
    return BUCKET_NEEDS_REVIEW


def bucket_conditions(bucket: str):
    """SQLAlchemy boolean condition selecting one bucket's rows.

    R2a: the bucket is now *persisted* (computed at write-time via ``classify()``),
    so this filters directly on the ``bucket`` column — collapsing the SQL twin and
    eliminating drift from the classifier logic, which lives in one place. The
    materiality routing is therefore already baked into the stored value.

    Lazy-imports the model to keep module import-time stdlib-only (schemas import classify()).
    """
    from app.models.reconciliation import ReconciliationResult as R

    if bucket in ALL_BUCKETS:
        return R.bucket == bucket
    raise ValueError(f"unknown bucket: {bucket}")
