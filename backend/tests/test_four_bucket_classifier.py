from decimal import Decimal

from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_MATCHES,
    BUCKET_RULES,
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_NEEDS_REVIEW,
    BULK_APPROVABLE_BUCKETS,
    classify,
)


def test_deterministic_clean_is_matches():
    assert classify("deterministic", None, Decimal("0")) == BUCKET_MATCHES


def test_deterministic_with_variance_type_is_auto_classifications():
    assert classify("deterministic", "amount_mismatch", Decimal("0.12")) == BUCKET_AUTO_CLASSIFICATIONS


def test_deterministic_with_nonzero_variance_amount_only_is_auto_classifications():
    assert classify("deterministic", None, Decimal("5.00")) == BUCKET_AUTO_CLASSIFICATIONS


def test_fuzzy_any_is_rules():
    assert classify("fuzzy", None, Decimal("0")) == BUCKET_RULES
    assert classify("fuzzy", "amount_mismatch", Decimal("5.11")) == BUCKET_RULES


def test_unmatched_is_needs_review():
    assert classify("unmatched", "missing_in_netsuite", Decimal("1203.68")) == BUCKET_NEEDS_REVIEW


def test_payout_exception_is_needs_review():
    assert classify("exception", "duplicate", Decimal("0")) == BUCKET_NEEDS_REVIEW


def test_unknown_match_type_is_needs_review():
    assert classify("weird_future_type", None, Decimal("0")) == BUCKET_NEEDS_REVIEW


def test_needs_review_not_bulk_approvable():
    assert BUCKET_NEEDS_REVIEW not in BULK_APPROVABLE_BUCKETS
    assert set(BULK_APPROVABLE_BUCKETS) == {BUCKET_MATCHES, BUCKET_RULES, BUCKET_AUTO_CLASSIFICATIONS}
