"""Tests for the recon confidence engine (R2 scorer).

TDD: this file was written BEFORE confidence_engine.py existed.
All tests must pass after the implementation is added.

Pure module — no DB, no network, no sockets.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.reconciliation.confidence_engine import (
    SCORER_VERSION,
    W_AMOUNT,
    W_TEMPORAL,
    WINDOW_DAYS,
    ConfidenceSignals,
    amount_score,
    composite,
    compute_signals,
    signals_to_evidence,
    temporal_score,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_scorer_version(self):
        assert SCORER_VERSION == "v1"

    def test_weights_sum_to_one(self):
        assert W_AMOUNT + W_TEMPORAL == Decimal("1.0")

    def test_weights_are_decimal(self):
        assert isinstance(W_AMOUNT, Decimal)
        assert isinstance(W_TEMPORAL, Decimal)

    def test_window_days(self):
        assert WINDOW_DAYS == 14


# ---------------------------------------------------------------------------
# amount_score
# ---------------------------------------------------------------------------


class TestAmountScore:
    def test_exact_match_returns_one(self):
        score = amount_score(Decimal("100.00"), Decimal("100.00"))
        assert score == Decimal("1.0000")

    def test_exact_match_is_decimal(self):
        score = amount_score(Decimal("100.00"), Decimal("100.00"))
        assert isinstance(score, Decimal)

    def test_small_relative_variance(self):
        # charge=100, deposit=100.30 → 1 - 0.30/100 = 0.9970
        score = amount_score(Decimal("100.00"), Decimal("100.30"))
        assert score == Decimal("0.9970")

    def test_large_variance_floors_at_zero(self):
        # charge=100, deposit=300 → 1 - 200/100 = -1 → clamped to 0
        score = amount_score(Decimal("100.00"), Decimal("300.00"))
        assert score == Decimal("0.0000")

    def test_charge_amount_zero_deposit_also_zero(self):
        # var=0 → 1.0
        score = amount_score(Decimal("0"), Decimal("0"))
        assert score == Decimal("1.0000")

    def test_charge_amount_zero_deposit_nonzero(self):
        # var>0 → 0.0
        score = amount_score(Decimal("0"), Decimal("50.00"))
        assert score == Decimal("0.0000")

    def test_result_is_always_decimal(self):
        for charge, deposit in [
            (Decimal("50"), Decimal("50")),
            (Decimal("0"), Decimal("0")),
            (Decimal("0"), Decimal("1")),
            (Decimal("100"), Decimal("200")),
        ]:
            assert isinstance(amount_score(charge, deposit), Decimal)

    def test_result_clamped_above_zero(self):
        # deposit much larger than charge → floor at 0, never negative
        score = amount_score(Decimal("10.00"), Decimal("1000.00"))
        assert score >= Decimal("0")

    def test_result_clamped_below_one(self):
        score = amount_score(Decimal("100.00"), Decimal("100.00"))
        assert score <= Decimal("1")

    def test_quantized_to_four_decimal_places(self):
        # variance ratio not evenly divisible — must still be 4dp
        score = amount_score(Decimal("100.00"), Decimal("99.99"))
        assert score == score.quantize(Decimal("0.0001"))

    def test_negative_direction_variance(self):
        # charge=100, deposit=99.70 → 1 - 0.30/100 = 0.9970 (same as +0.30 side)
        score = amount_score(Decimal("100.00"), Decimal("99.70"))
        assert score == Decimal("0.9970")


# ---------------------------------------------------------------------------
# temporal_score
# ---------------------------------------------------------------------------


class TestTemporalScore:
    def test_same_day_returns_one(self):
        d = date(2024, 1, 15)
        score = temporal_score(d, d)
        assert score == Decimal("1.0000")

    def test_mid_window_7_days(self):
        # gap=7 → 1 - 7/14 = 0.5000
        score = temporal_score(date(2024, 1, 1), date(2024, 1, 8))
        assert score == Decimal("0.5000")

    def test_exactly_14_days_returns_zero(self):
        score = temporal_score(date(2024, 1, 1), date(2024, 1, 15))
        assert score == Decimal("0.0000")

    def test_beyond_14_days_clamped_to_zero(self):
        # gap=30 → 1 - 30/14 = negative → clamped to 0
        score = temporal_score(date(2024, 1, 1), date(2024, 1, 31))
        assert score == Decimal("0.0000")

    def test_order_independence(self):
        # swapping dates gives the same score
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 5)
        assert temporal_score(d1, d2) == temporal_score(d2, d1)

    def test_charge_date_none_returns_none(self):
        assert temporal_score(None, date(2024, 1, 1)) is None

    def test_deposit_date_none_returns_none(self):
        assert temporal_score(date(2024, 1, 1), None) is None

    def test_both_dates_none_returns_none(self):
        assert temporal_score(None, None) is None

    def test_result_is_decimal_when_dates_present(self):
        score = temporal_score(date(2024, 1, 1), date(2024, 1, 3))
        assert isinstance(score, Decimal)

    def test_result_clamped_between_zero_and_one(self):
        # never negative, never > 1
        for gap in [0, 1, 7, 14, 30, 100]:
            d1 = date(2024, 1, 1)
            d2 = d1 + timedelta(days=gap)
            score = temporal_score(d1, d2)
            assert Decimal("0") <= score <= Decimal("1")

    def test_one_day_gap(self):
        # 1 - 1/14 = 13/14
        expected = (Decimal(1) - Decimal(1) / Decimal(14)).quantize(Decimal("0.0001"))
        score = temporal_score(date(2024, 6, 1), date(2024, 6, 2))
        assert score == expected


# ---------------------------------------------------------------------------
# composite
# ---------------------------------------------------------------------------


class TestComposite:
    def test_both_present_amount_one_temporal_half(self):
        # 0.6*1 + 0.4*0.5 = 0.8000
        score = composite(Decimal("1.0000"), Decimal("0.5000"))
        assert score == Decimal("0.8000")

    def test_temporal_none_returns_amount_exactly(self):
        amt = Decimal("0.7500")
        score = composite(amt, None)
        assert score == amt

    def test_temporal_none_returns_decimal(self):
        score = composite(Decimal("0.9000"), None)
        assert isinstance(score, Decimal)

    def test_result_stays_within_zero_one(self):
        for a, t in [
            (Decimal("1.0000"), Decimal("1.0000")),
            (Decimal("0.0000"), Decimal("0.0000")),
            (Decimal("0.5000"), Decimal("0.5000")),
        ]:
            score = composite(a, t)
            assert Decimal("0") <= score <= Decimal("1")

    def test_result_quantized_to_four_decimal_places(self):
        score = composite(Decimal("0.3333"), Decimal("0.6667"))
        assert score == score.quantize(Decimal("0.0001"))

    def test_both_zero(self):
        assert composite(Decimal("0.0000"), Decimal("0.0000")) == Decimal("0.0000")

    def test_both_one(self):
        assert composite(Decimal("1.0000"), Decimal("1.0000")) == Decimal("1.0000")


# ---------------------------------------------------------------------------
# compute_signals
# ---------------------------------------------------------------------------


class TestComputeSignals:
    def test_returns_confidence_signals_instance(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 1),
        )
        assert isinstance(sig, ConfidenceSignals)

    def test_scorer_version_is_v1(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 1),
        )
        assert sig.scorer_version == "v1"

    def test_weights_field(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 1),
        )
        assert sig.weights == {"amount": Decimal("0.6"), "temporal": Decimal("0.4")}

    def test_all_fields_populated(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 1),
        )
        assert sig.amount_score is not None
        assert sig.temporal_score is not None
        assert sig.composite is not None
        assert sig.scorer_version is not None
        assert sig.weights is not None

    def test_missing_date_yields_none_temporal(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            None,
            None,
        )
        assert sig.temporal_score is None

    def test_missing_date_composite_equals_amount_score(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            None,
            None,
        )
        assert sig.composite == sig.amount_score

    def test_exact_match_exact_date(self):
        sig = compute_signals(
            Decimal("250.00"),
            Decimal("250.00"),
            date(2024, 3, 10),
            date(2024, 3, 10),
        )
        assert sig.amount_score == Decimal("1.0000")
        assert sig.temporal_score == Decimal("1.0000")
        assert sig.composite == Decimal("1.0000")

    def test_frozen_dataclass_immutable(self):
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 1),
        )
        with pytest.raises((AttributeError, TypeError)):
            sig.composite = Decimal("0.5")  # type: ignore[misc]

    def test_weights_mapping_is_immutable(self):
        # frozen=True does NOT protect a mutable dict value — weights must be a
        # read-only mapping so persisted evidence can't be corrupted downstream.
        sig = compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 1),
        )
        with pytest.raises(TypeError):
            sig.weights["amount"] = Decimal("0.99")  # type: ignore[index]


# ---------------------------------------------------------------------------
# signals_to_evidence
# ---------------------------------------------------------------------------


class TestSignalsToEvidence:
    def _make_signals_full(self) -> ConfidenceSignals:
        return compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            date(2024, 1, 1),
            date(2024, 1, 5),
        )

    def _make_signals_no_dates(self) -> ConfidenceSignals:
        return compute_signals(
            Decimal("100.00"),
            Decimal("100.00"),
            None,
            None,
        )

    def test_json_serializable(self):
        ev = signals_to_evidence(self._make_signals_full())
        # Must not raise
        json.dumps(ev)

    def test_no_decimal_values(self):
        ev = signals_to_evidence(self._make_signals_full())
        for v in ev.values():
            assert not isinstance(v, Decimal), f"Decimal found: {v!r}"
        # nested weights sub-dict must also be Decimal-free
        for v in ev["weights"].values():
            assert not isinstance(v, Decimal), f"Decimal found in weights: {v!r}"

    def test_no_date_values(self):
        ev = signals_to_evidence(self._make_signals_full())
        for v in ev.values():
            assert not isinstance(v, date), f"date found: {v!r}"
        # nested weights sub-dict must also be date-free
        for v in ev["weights"].values():
            assert not isinstance(v, date), f"date found in weights: {v!r}"

    def test_scores_serialized_as_strings(self):
        ev = signals_to_evidence(self._make_signals_full())
        assert isinstance(ev["amount_score"], str)
        assert isinstance(ev["composite"], str)

    def test_temporal_score_string_when_present(self):
        ev = signals_to_evidence(self._make_signals_full())
        assert isinstance(ev["temporal_score"], str)

    def test_temporal_score_none_when_unavailable(self):
        ev = signals_to_evidence(self._make_signals_no_dates())
        assert ev["temporal_score"] is None

    def test_scorer_version_preserved(self):
        ev = signals_to_evidence(self._make_signals_full())
        assert ev["scorer_version"] == "v1"

    def test_weights_are_string_values(self):
        ev = signals_to_evidence(self._make_signals_full())
        weights = ev["weights"]
        assert weights["amount"] == "0.6"
        assert weights["temporal"] == "0.4"

    def test_json_roundtrip(self):
        ev = signals_to_evidence(self._make_signals_full())
        serialized = json.dumps(ev)
        restored = json.loads(serialized)
        assert restored["scorer_version"] == "v1"
        assert restored["weights"]["amount"] == "0.6"

    def test_json_roundtrip_no_dates(self):
        ev = signals_to_evidence(self._make_signals_no_dates())
        serialized = json.dumps(ev)
        restored = json.loads(serialized)
        assert restored["temporal_score"] is None
