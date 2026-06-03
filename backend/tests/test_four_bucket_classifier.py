from decimal import Decimal

import pytest

from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
    BULK_APPROVABLE_BUCKETS,
    _is_material,
    bucket_conditions,
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


# ---------------------------------------------------------------------------
# R2a — materiality routing (optional kwargs; None ⇒ R1 behavior)
# ---------------------------------------------------------------------------


def test_none_thresholds_reproduce_r1_matrix_exactly():
    """With both thresholds None, classify() is byte-identical to the R1 waterfall."""
    # deterministic clean → matches
    assert classify("deterministic", None, Decimal("0")) == BUCKET_MATCHES
    # deterministic + variance_type → auto_classifications (any magnitude)
    assert classify("deterministic", "amount_mismatch", Decimal("999999")) == BUCKET_AUTO_CLASSIFICATIONS
    # deterministic + nonzero variance amount only → auto_classifications
    assert classify("deterministic", None, Decimal("5.00")) == BUCKET_AUTO_CLASSIFICATIONS
    # fuzzy → rules (any variance magnitude)
    assert classify("fuzzy", None, Decimal("0")) == BUCKET_RULES
    assert classify("fuzzy", "amount_mismatch", Decimal("999999")) == BUCKET_RULES
    # unmatched / exception / unknown → needs_review
    assert classify("unmatched", "missing_in_netsuite", Decimal("1203.68")) == BUCKET_NEEDS_REVIEW
    assert classify("exception", "duplicate", Decimal("0")) == BUCKET_NEEDS_REVIEW
    assert classify("weird_future_type", None, Decimal("0")) == BUCKET_NEEDS_REVIEW


def test_none_thresholds_with_matched_amount_still_r1():
    """matched_amount provided but no thresholds → still R1 (immaterial path)."""
    assert (
        classify("deterministic", "amount_mismatch", Decimal("5.00"), matched_amount=Decimal("10.00"))
        == BUCKET_AUTO_CLASSIFICATIONS
    )
    assert classify("fuzzy", "amount_mismatch", Decimal("5.00"), matched_amount=Decimal("10.00")) == BUCKET_RULES


def test_material_deterministic_variance_routes_needs_review():
    # abs variance 60 > abs threshold 50 → material → needs_review
    assert (
        classify(
            "deterministic",
            "amount_mismatch",
            Decimal("60.00"),
            materiality_abs=Decimal("50"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("100000.00"),
        )
        == BUCKET_NEEDS_REVIEW
    )


def test_material_fuzzy_variance_routes_needs_review():
    assert (
        classify(
            "fuzzy",
            "amount_mismatch",
            Decimal("60.00"),
            materiality_abs=Decimal("50"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("100000.00"),
        )
        == BUCKET_NEEDS_REVIEW
    )


def test_immaterial_deterministic_variance_stays_auto_classifications():
    # abs variance 5 ≤ 50 and 5/10000 = 0.0005 ≤ 0.01 → immaterial → auto_classifications
    assert (
        classify(
            "deterministic",
            "amount_mismatch",
            Decimal("5.00"),
            materiality_abs=Decimal("50"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("10000.00"),
        )
        == BUCKET_AUTO_CLASSIFICATIONS
    )


def test_immaterial_fuzzy_variance_stays_rules():
    assert (
        classify(
            "fuzzy",
            "amount_mismatch",
            Decimal("5.00"),
            materiality_abs=Decimal("50"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("10000.00"),
        )
        == BUCKET_RULES
    )


def test_pct_path_material_without_matched_amount_is_not_material():
    # pct threshold set but no matched_amount → pct branch cannot apply.
    # abs threshold here is large so abs branch also misses → immaterial.
    assert (
        classify(
            "deterministic",
            "amount_mismatch",
            Decimal("5.00"),
            materiality_abs=Decimal("100"),
            materiality_pct=Decimal("0.01"),
            matched_amount=None,
        )
        == BUCKET_AUTO_CLASSIFICATIONS
    )


def test_pct_path_material_with_matched_amount_routes_needs_review():
    # abs variance 2 ≤ 100 (abs miss); 2/100 = 0.02 > 0.01 → pct material → needs_review
    assert (
        classify(
            "fuzzy",
            "amount_mismatch",
            Decimal("2.00"),
            materiality_abs=Decimal("100"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("100.00"),
        )
        == BUCKET_NEEDS_REVIEW
    )


def test_material_routing_does_not_touch_clean_or_unmatched():
    # clean deterministic stays matches even with thresholds set (no variance)
    assert (
        classify(
            "deterministic",
            None,
            Decimal("0"),
            materiality_abs=Decimal("50"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("100.00"),
        )
        == BUCKET_MATCHES
    )
    # unmatched stays needs_review regardless of thresholds
    assert (
        classify(
            "unmatched",
            "missing_in_netsuite",
            Decimal("1203.68"),
            materiality_abs=Decimal("50"),
            materiality_pct=Decimal("0.01"),
            matched_amount=Decimal("1203.68"),
        )
        == BUCKET_NEEDS_REVIEW
    )


# ---------------------------------------------------------------------------
# _is_material unit cases
# ---------------------------------------------------------------------------


def test_is_material_both_none_is_false():
    # Both thresholds None → never material (⇒ R1 preserved exactly)
    assert _is_material(Decimal("99999"), Decimal("1.00"), None, None) is False


def test_is_material_abs_branch():
    assert _is_material(Decimal("60"), Decimal("100000"), Decimal("50"), None) is True
    assert _is_material(Decimal("50"), Decimal("100000"), Decimal("50"), None) is False  # strictly greater
    assert _is_material(Decimal("-60"), Decimal("100000"), Decimal("50"), None) is True  # abs


def test_is_material_pct_branch():
    assert _is_material(Decimal("2"), Decimal("100"), None, Decimal("0.01")) is True  # 0.02 > 0.01
    assert _is_material(Decimal("1"), Decimal("100"), None, Decimal("0.01")) is False  # 0.01 not > 0.01
    assert _is_material(Decimal("-2"), Decimal("-100"), None, Decimal("0.01")) is True  # abs both


def test_is_material_pct_branch_requires_matched_amount():
    assert _is_material(Decimal("2"), None, None, Decimal("0.01")) is False
    assert _is_material(Decimal("2"), Decimal("0"), None, Decimal("0.01")) is False  # no div-by-zero


def test_is_material_or_semantics():
    # abs misses, pct hits → True
    assert _is_material(Decimal("2"), Decimal("100"), Decimal("100"), Decimal("0.01")) is True
    # abs hits, pct misses → True
    assert _is_material(Decimal("60"), Decimal("1000000"), Decimal("50"), Decimal("0.01")) is True


# ---------------------------------------------------------------------------
# bucket_conditions — persisted-column filter
# ---------------------------------------------------------------------------


def test_bucket_conditions_unknown_raises():
    with pytest.raises(ValueError):
        bucket_conditions("not_a_bucket")
