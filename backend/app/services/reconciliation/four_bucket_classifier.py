"""Pure read-side four-bucket classifier for the recon reviewer (R1).

Maps each ReconciliationResult to exactly one bucket from match_type + variance
presence. NO confidence threshold (deferred to R2), NO collision detection
(deferred to R2), NO matching/engine behavior change. Stdlib-only so schemas may
import it safely; the SQL twin lazy-imports the model.
"""

from __future__ import annotations

from decimal import Decimal

BUCKET_MATCHES = "matches"
BUCKET_RULES = "rules"
BUCKET_AUTO_CLASSIFICATIONS = "auto_classifications"
BUCKET_NEEDS_REVIEW = "needs_review"

ALL_BUCKETS = (BUCKET_MATCHES, BUCKET_RULES, BUCKET_AUTO_CLASSIFICATIONS, BUCKET_NEEDS_REVIEW)
BULK_APPROVABLE_BUCKETS = (BUCKET_MATCHES, BUCKET_RULES, BUCKET_AUTO_CLASSIFICATIONS)


def _has_variance(variance_type: str | None, variance_amount: Decimal | None) -> bool:
    if variance_type is not None:
        return True
    return variance_amount is not None and variance_amount != 0


def classify(match_type: str, variance_type: str | None, variance_amount: Decimal | None) -> str:
    """Return the bucket id for one result's fields. Total + deterministic."""
    if match_type == "deterministic":
        return BUCKET_AUTO_CLASSIFICATIONS if _has_variance(variance_type, variance_amount) else BUCKET_MATCHES
    if match_type == "fuzzy":
        return BUCKET_RULES
    # unmatched, exception (payout dup), or any unknown future type → safe default
    return BUCKET_NEEDS_REVIEW
