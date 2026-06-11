"""Integration tests for reconciliation job runner."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.reconciliation import DepositRecord, MatchCandidate, PayoutRecord
from app.services.reconciliation.confidence_engine import compute_signals
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
)
from app.services.reconciliation.recon_job import ReconJobRunner


@pytest.fixture
def mock_db():
    """Mock async database session."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def sample_payouts() -> list[PayoutRecord]:
    return [
        PayoutRecord(
            id=str(uuid.uuid4()),
            source_id="po_job01",
            amount=Decimal("1000.00"),
            net_amount=Decimal("970.00"),
            fee_amount=Decimal("30.00"),
            currency="USD",
            arrival_date=date(2026, 3, 10),
        ),
        PayoutRecord(
            id=str(uuid.uuid4()),
            source_id="po_job02",
            amount=Decimal("500.00"),
            net_amount=Decimal("485.00"),
            fee_amount=Decimal("15.00"),
            currency="USD",
            arrival_date=date(2026, 3, 11),
        ),
    ]


@pytest.fixture
def sample_deposits() -> list[DepositRecord]:
    return [
        DepositRecord(
            id=str(uuid.uuid4()),
            netsuite_internal_id="20001",
            amount=Decimal("970.00"),
            currency="USD",
            transaction_date=date(2026, 3, 10),
            memo="Stripe payout po_job01",
            related_payout_id="po_job01",
        ),
    ]


class TestReconJobRunner:
    @pytest.mark.asyncio
    async def test_run_produces_summary(self, mock_db, sample_payouts, sample_deposits):
        """Job runner should return a summary with match counts."""
        runner = ReconJobRunner(db=mock_db, tenant_id=str(uuid.uuid4()))

        with (
            patch.object(runner, "_fetch_payouts", return_value=sample_payouts),
            patch.object(runner, "_fetch_deposits", return_value=sample_deposits),
            # _store_results is typed -> list[str] (the per-candidate buckets); honor
            # that contract so the run() rollup .count() calls have a real list.
            patch.object(runner, "_store_results", return_value=[]),
        ):
            summary = await runner.run(
                date_from=date(2026, 3, 1),
                date_to=date(2026, 3, 31),
            )

        assert summary.total_payouts == 2
        assert summary.total_deposits == 1
        assert summary.matched_count >= 1
        assert summary.status == "completed"

    @pytest.mark.asyncio
    async def test_run_stores_results(self, mock_db, sample_payouts, sample_deposits):
        """Job runner should call _store_results with match candidates."""
        runner = ReconJobRunner(db=mock_db, tenant_id=str(uuid.uuid4()))

        stored_results = []

        async def capture_store(run_id, candidates):
            stored_results.extend(candidates)
            # Mirror the real _store_results contract: returns the per-candidate buckets.
            return ["needs_review"] * len(candidates)

        with (
            patch.object(runner, "_fetch_payouts", return_value=sample_payouts),
            patch.object(runner, "_fetch_deposits", return_value=sample_deposits),
            patch.object(runner, "_store_results", side_effect=capture_store),
        ):
            await runner.run(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))

        # Should have at least 1 matched + 1 unmatched
        assert len(stored_results) >= 2

    @pytest.mark.asyncio
    async def test_run_with_subsidiary_filter(self, mock_db):
        """Job runner should pass subsidiary_id to fetch methods."""
        runner = ReconJobRunner(db=mock_db, tenant_id=str(uuid.uuid4()))

        with (
            patch.object(runner, "_fetch_payouts", return_value=[]) as mock_fetch_p,
            patch.object(runner, "_fetch_deposits", return_value=[]),
            patch.object(runner, "_store_results", return_value=[]),
        ):
            await runner.run(
                date_from=date(2026, 3, 1),
                date_to=date(2026, 3, 31),
                subsidiary_id="sub_123",
            )

        mock_fetch_p.assert_called_once()
        call_kwargs = mock_fetch_p.call_args
        assert call_kwargs[1].get("subsidiary_id") == "sub_123" or "sub_123" in str(call_kwargs)


# ---------------------------------------------------------------------------
# R2 advisory confidence convergence (Task C) — _store_results persists the
# amount+temporal composite for matched candidates, NOT the engine match-tier
# ladder; the ladder still (and only) drives ``status``. No DB needed: db.add
# captures the ReconciliationResult rows (mirrors test_order_recon_job.py).
# ---------------------------------------------------------------------------


def _store_mock_db() -> AsyncMock:
    """Mock db for _store_results: capture add() calls; no TenantConfig row so
    the materiality loader falls back to the $50 / 1% defaults."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    no_config = MagicMock()
    no_config.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=no_config)
    return db


def _payout(
    *,
    source_id: str = "po_adv",
    net_amount: Decimal = Decimal("970.00"),
    arrival_date: date | None = date(2026, 3, 10),
) -> PayoutRecord:
    return PayoutRecord(
        id=str(uuid.uuid4()),
        source_id=source_id,
        amount=net_amount + Decimal("30.00"),
        net_amount=net_amount,
        fee_amount=Decimal("30.00"),
        currency="USD",
        arrival_date=arrival_date,
    )


def _deposit(
    *,
    amount: Decimal = Decimal("970.00"),
    transaction_date: date | None = date(2026, 3, 10),
) -> DepositRecord:
    return DepositRecord(
        id=str(uuid.uuid4()),
        netsuite_internal_id="20001",
        amount=amount,
        currency="USD",
        transaction_date=transaction_date,
        memo="Stripe payout",
        related_payout_id="po_adv",
    )


def _candidate(
    *,
    payout: PayoutRecord,
    deposits: list[DepositRecord],
    match_type: str,
    confidence: Decimal = Decimal("1.0"),
    variance_amount: Decimal = Decimal("0"),
    variance_type: str | None = None,
    match_rule: str | None = "exact_payout_id",
) -> MatchCandidate:
    return MatchCandidate(
        payout=payout,
        deposits=deposits,
        match_type=match_type,
        confidence=confidence,
        variance_amount=variance_amount,
        variance_type=variance_type,
        match_rule=match_rule,
    )


async def _store_one(candidate: MatchCandidate):
    """Run _store_results for one candidate; return the captured ORM row."""
    db = _store_mock_db()
    runner = ReconJobRunner(db=db, tenant_id=str(uuid.uuid4()))
    await runner._store_results(uuid.uuid4(), [candidate])
    assert db.add.called
    return db.add.call_args_list[0][0][0]


class TestStoreResultsAdvisoryConfidence:
    """The persisted ``confidence`` carries the R2 advisory composite; the
    engine ladder drives ``status`` only — decoupled BOTH directions (I8)."""

    @pytest.mark.asyncio
    async def test_low_composite_does_not_demote_auto_matched(self):
        """I8 direction 1: gap-14 temporal → composite 0.6 persisted, but the
        deterministic ladder 1.0 still yields auto_matched (no demotion)."""
        candidate = _candidate(
            payout=_payout(arrival_date=date(2026, 3, 10)),
            deposits=[_deposit(transaction_date=date(2026, 3, 24))],  # gap 14d
            match_type="deterministic",
            confidence=Decimal("1.0"),
        )

        result = await _store_one(candidate)

        # amount 1.0, temporal 0.0 → composite 0.6 — NOT the ladder 1.0
        assert result.confidence == Decimal("0.6000")
        # status still ladder-derived (1.0 >= 0.95) — low composite must not demote
        assert result.status == "auto_matched"
        # bucket logic untouched: deterministic + no variance → matches
        assert result.bucket == BUCKET_MATCHES
        # sub-scores captured for calibration
        signals = result.evidence["confidence_signals"]
        assert signals["amount_score"] == "1.0000"
        assert signals["temporal_score"] == "0.0000"
        assert signals["composite"] == "0.6000"
        assert signals["scorer_version"] == "v1"
        assert signals["weights"] == {"amount": "0.6", "temporal": "0.4"}

    @pytest.mark.asyncio
    async def test_high_composite_does_not_promote_fuzzy(self):
        """I8 direction 2: same-day exact-amount → composite 1.0 persisted, but
        the fuzzy ladder 0.80 still yields suggested (no promotion)."""
        candidate = _candidate(
            payout=_payout(arrival_date=date(2026, 3, 10)),
            deposits=[_deposit(transaction_date=date(2026, 3, 10))],  # same day
            match_type="fuzzy",
            confidence=Decimal("0.80"),
            match_rule="amount_date_window",
        )

        result = await _store_one(candidate)

        # perfect amount + same-day → composite 1.0 — NOT the ladder 0.80
        assert result.confidence == Decimal("1.0000")
        # status still ladder-derived (0.80 in [0.75, 0.95)) — high composite must not promote
        assert result.status == "suggested"
        # bucket logic untouched: fuzzy + no variance → rules
        assert result.bucket == BUCKET_RULES

    @pytest.mark.asyncio
    async def test_exception_duplicate_keeps_ladder_value_no_signals(self):
        """Duplicate exception (>=2 deposits EACH claiming the SAME payout) is
        NOT a split — summing the deposits would double-count and collapse the
        amount score. Exception rows keep the engine ladder value (0.60), like
        unmatched, and capture NO confidence_signals."""
        candidate = _candidate(
            payout=_payout(arrival_date=date(2026, 3, 10)),
            deposits=[
                _deposit(transaction_date=date(2026, 3, 10)),
                _deposit(transaction_date=date(2026, 3, 10)),  # duplicate of the same payout
            ],
            match_type="exception",
            confidence=Decimal("0.60"),
            variance_amount=Decimal("970.00"),
            variance_type="duplicate",
            match_rule="duplicate_detection",
        )

        result = await _store_one(candidate)

        # Ladder 0.60 persisted — NOT a composite (summed duplicates would give
        # amount 0.0 + same-day temporal 1.0 → composite 0.4000).
        assert result.confidence == Decimal("0.60")
        # status still ladder-derived (0.60 < 0.75 → pending)
        assert result.status == "pending"
        # exception → needs_review (classifier safe default) — bucket logic untouched
        assert result.bucket == BUCKET_NEEDS_REVIEW
        # no signals captured: the pair-score is meaningless for duplicates
        assert "confidence_signals" not in result.evidence

    @pytest.mark.asyncio
    async def test_unmatched_keeps_engine_zero(self):
        """Unmatched candidates keep the engine value (0); no signals captured."""
        candidate = _candidate(
            payout=_payout(net_amount=Decimal("50.00")),
            deposits=[],
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("50.00"),
            variance_type="missing",
            match_rule=None,
        )

        result = await _store_one(candidate)

        assert result.confidence == Decimal("0")
        assert result.status == "pending"
        assert result.bucket == BUCKET_NEEDS_REVIEW
        assert "confidence_signals" not in result.evidence

    @pytest.mark.asyncio
    async def test_split_payout_uses_summed_amount_and_latest_date(self):
        """Multi-deposit (split payout): the amount signal scores the SUMMED
        deposit amount; the temporal signal uses the LATEST deposit date."""
        candidate = _candidate(
            payout=_payout(arrival_date=date(2026, 3, 10)),
            deposits=[
                _deposit(amount=Decimal("500.00"), transaction_date=date(2026, 3, 10)),
                _deposit(amount=Decimal("470.00"), transaction_date=date(2026, 3, 12)),
            ],
            match_type="fuzzy",
            confidence=Decimal("0.80"),
            match_rule="split_payout",
        )

        result = await _store_one(candidate)

        expected = compute_signals(
            charge_amount=Decimal("970.00"),
            deposit_amount=Decimal("970.00"),  # 500 + 470 summed
            charge_date=date(2026, 3, 10),
            deposit_date=date(2026, 3, 12),  # latest of the split
        )
        assert result.confidence == expected.composite

        signals = result.evidence["confidence_signals"]
        # 1.0000 proves the SUM was scored (first-deposit-only would be ~0.5155)
        assert signals["amount_score"] == "1.0000"
        # gap 2 (latest date) → 0.8571; first-deposit date would give "1.0000"
        assert signals["temporal_score"] == "0.8571"

    @pytest.mark.asyncio
    async def test_deposit_date_none_falls_back_to_amount_only(self):
        """All deposit dates None → temporal unavailable; composite = amount-only."""
        candidate = _candidate(
            payout=_payout(arrival_date=date(2026, 3, 10)),
            deposits=[_deposit(transaction_date=None)],
            match_type="deterministic",
            confidence=Decimal("1.0"),
        )

        result = await _store_one(candidate)

        assert result.confidence == Decimal("1.0000")  # amount-only fallback
        assert result.status == "auto_matched"
        signals = result.evidence["confidence_signals"]
        assert signals["temporal_score"] is None

    @pytest.mark.asyncio
    async def test_mixed_none_dates_use_latest_known_date(self):
        """A dateless deposit drops out of the max; the latest KNOWN date scores."""
        candidate = _candidate(
            payout=_payout(arrival_date=date(2026, 3, 10)),
            deposits=[
                _deposit(amount=Decimal("500.00"), transaction_date=None),
                _deposit(amount=Decimal("470.00"), transaction_date=date(2026, 3, 12)),
            ],
            match_type="fuzzy",
            confidence=Decimal("0.80"),
            match_rule="split_payout",
        )

        result = await _store_one(candidate)

        signals = result.evidence["confidence_signals"]
        assert signals["temporal_score"] == "0.8571"  # gap 2 from the known date
