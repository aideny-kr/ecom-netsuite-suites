from decimal import Decimal

from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
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


from app.schemas.reconciliation import ReconResultResponse


def _result_payload(**over):
    base = dict(
        id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        payout_id=None,
        deposit_id=None,
        match_type="deterministic",
        confidence=Decimal("1.0"),
        status="auto_matched",
        stripe_amount=Decimal("10.00"),
        netsuite_amount=Decimal("10.00"),
        variance_amount=Decimal("0"),
        variance_type=None,
        variance_explanation=None,
        currency="USD",
        match_rule="order_reference_exact",
        approved_by=None,
        approved_at=None,
        created_at="2026-06-01T00:00:00Z",
    )
    base.update(over)
    return base


def test_response_exposes_bucket_matches():
    resp = ReconResultResponse(**_result_payload())
    assert resp.bucket == BUCKET_MATCHES
    assert "bucket" in resp.model_dump()


def test_response_bucket_auto_classifications_on_variance():
    resp = ReconResultResponse(**_result_payload(variance_type="amount_mismatch", variance_amount=Decimal("0.12")))
    assert resp.bucket == BUCKET_AUTO_CLASSIFICATIONS
