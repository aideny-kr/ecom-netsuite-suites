"""Pydantic schemas for reconciliation engine."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, model_validator

from app.services.reconciliation.four_bucket_classifier import classify

# Coerce UUID objects to str for response schemas
StrFromUUID = Annotated[str, BeforeValidator(lambda v: str(v) if isinstance(v, UUID) else v)]

# ---------------------------------------------------------------------------
# Enums / Literals
# ---------------------------------------------------------------------------
MatchType = Literal["deterministic", "fuzzy", "unmatched", "exception"]
VarianceType = Literal["fees", "fx_rounding", "timing", "missing", "duplicate", "chargeback", "manual_adjustment"]
ResultStatus = Literal[
    "pending",
    "auto_matched",
    "suggested",
    "approved",
    "rejected",
    "investigating",
    "locked",
    "carried_forward",
]
RunStatus = Literal["pending", "running", "completed", "failed", "closed"]

ResolutionAction = Literal[
    "book_fee_line",
    "create_and_apply_deposit",
    "apply_deposit",
    "credit_memo_refund",
    "void_duplicate",
    "writeoff_je",
    "carry_forward",
    "needs_human",
]
ProposalStatus = Literal[
    "proposed",
    "approved",
    "posting",
    "posted",
    "rejected",
    "post_failed",
    "superseded",
]
PostFailureReason = Literal[
    "period_locked",
    "period_closed",
    "connection",
    "netsuite_validation",
    "netsuite_error",
    "guard_tripped",
]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class ReconRunCreate(BaseModel):
    date_from: date
    date_to: date
    subsidiary_id: str | None = None
    payout_ids: list[str] | None = Field(default=None, description="Specific payout source_ids to reconcile")
    match_level: Literal["order", "payout"] = Field(
        default="order", description="Matching granularity: 'order' (charge-level) or 'payout' (aggregate)"
    )


class ReconResultApprove(BaseModel):
    result_id: str
    notes: str | None = None


class ReconCloseRequest(BaseModel):
    period: str = Field(description="Period to close, e.g. '2026-03'")
    subsidiary_id: str | None = None


# ---------------------------------------------------------------------------
# Internal data types (used by matching engine, not exposed via API)
# ---------------------------------------------------------------------------
class PayoutRecord(BaseModel):
    """Lightweight view of a canonical Payout for matching."""

    id: str
    source_id: str
    amount: Decimal
    net_amount: Decimal
    fee_amount: Decimal
    currency: str
    arrival_date: date | None
    subsidiary_id: str | None = None

    model_config = {"from_attributes": True}


class DepositRecord(BaseModel):
    """Lightweight view of a NetSuite posting (deposit) for matching."""

    id: str
    netsuite_internal_id: str | None
    amount: Decimal
    currency: str
    transaction_date: date | None
    memo: str | None
    related_payout_id: str | None
    subsidiary_id: str | None = None

    model_config = {"from_attributes": True}


class MatchCandidate(BaseModel):
    """A proposed match between a payout and one or more deposits."""

    payout: PayoutRecord
    deposits: list[DepositRecord]
    match_type: MatchType
    confidence: Decimal
    variance_amount: Decimal = Decimal("0")
    variance_type: VarianceType | None = None
    variance_explanation: str | None = None
    match_rule: str | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------
class ReconResultResponse(BaseModel):
    id: StrFromUUID
    run_id: StrFromUUID
    payout_id: StrFromUUID | None
    deposit_id: StrFromUUID | None
    match_type: str
    confidence: Decimal
    status: str
    stripe_amount: Decimal | None
    netsuite_amount: Decimal | None
    variance_amount: Decimal
    variance_type: str | None
    variance_explanation: str | None
    currency: str
    match_rule: str | None
    approved_by: StrFromUUID | None = None
    approved_at: datetime | None = None
    # Persisted four-bucket classification (R2a). Populated from the stored
    # ``ReconciliationResult.bucket`` column via from_attributes; the materiality
    # routing is already baked into that value at write-time. Falls back to the
    # legacy ``classify()`` (no materiality) when the stored value is missing/None
    # — e.g. rows written before the column existed.
    bucket: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _fallback_bucket(self) -> ReconResultResponse:
        if self.bucket is None:
            self.bucket = classify(self.match_type, self.variance_type, self.variance_amount)
        return self

    model_config = {"from_attributes": True}


class ReconRunResponse(BaseModel):
    id: StrFromUUID
    tenant_id: StrFromUUID
    date_from: date
    date_to: date
    subsidiary_id: str | None
    status: str
    total_payouts: int
    total_deposits: int
    matched_count: int
    exception_count: int
    unmatched_count: int
    total_variance: Decimal
    # Per-bucket rollup counts (R2a) for the runs-list view. Default 0 for runs
    # predating the columns.
    matches_count: int = 0
    rules_count: int = 0
    auto_classifications_count: int = 0
    needs_review_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class ReconRunSummary(BaseModel):
    run_id: str
    status: str
    total_payouts: int
    total_deposits: int
    matched_count: int
    exception_count: int
    unmatched_count: int
    total_variance: Decimal
    match_rate: Decimal = Field(description="Percentage of payouts matched (0-100)")


# ---------------------------------------------------------------------------
# Four-bucket reviewer schemas (R1)
# ---------------------------------------------------------------------------
class ReconBucketCount(BaseModel):
    count: int
    total_variance: Decimal


class ReconCloseReadiness(BaseModel):
    """PERIOD-scoped close-readiness counts over ALL runs a close will touch.

    ``POST /close/{period}`` closes EVERY completed run whose date range falls
    inside the month, so the FE CloseChecklist's gate must aggregate over that
    same scope (``close_scope.closeable_runs_conditions``) — never a single
    selected run (R3-A). Every count keys on the authoritative
    ``status``/``bucket`` only, never the advisory confidence composite (the
    R2 decoupling pattern).

    - ``runs_in_scope``: how many runs ``close_period(period)`` would close
    - ``in_scope_run_ids``: the ids of exactly those runs (sorted). R4-A: lets
      the FE verify the SELECTED run is actually inside the close scope — a
      month-spanning run derives a period it is NOT closeable under, and with
      zero in-scope runs every count is vacuously zero, so counts alone would
      fail OPEN.
    - ``open_exceptions``: status='pending' AND match_type != 'unmatched'
    - ``suggested``: status='suggested'
    - ``left_for_review``: status='auto_matched' AND bucket='needs_review' —
      ``close_scope.left_for_review_conditions``: rows close deliberately
      leaves unlocked for human review (HITL).
    - ``carried_forward``: status='carried_forward' — an acknowledged
      reconciling item (timing group-approved). Non-blocking (not
      ``open_exceptions``) and never locked by ``close_period``. Default 0
      keeps older API clients working.
    """

    period: str
    runs_in_scope: int
    in_scope_run_ids: list[str]
    open_exceptions: int
    suggested: int
    left_for_review: int
    carried_forward: int = 0


class ReconBucketSummary(BaseModel):
    run_id: str
    matches: ReconBucketCount
    rules: ReconBucketCount
    auto_classifications: ReconBucketCount
    needs_review: ReconBucketCount


class ReconBucketApprove(BaseModel):
    bucket: str
    notes: str | None = None


class ReconBucketApproveResult(BaseModel):
    run_id: str
    bucket: str
    approved_count: int
    skipped_count: int
    correlation_id: str


# ---------------------------------------------------------------------------
# Resolution proposal schemas (summary-first rework, Phase 1)
# ---------------------------------------------------------------------------
class ResolutionProposalResponse(BaseModel):
    id: StrFromUUID
    run_id: StrFromUUID
    result_id: StrFromUUID
    root_cause: str
    action: str
    booking_vehicle: str
    group_key: str
    source: str
    narrative: str
    proposed_amount: Decimal
    currency: str
    above_materiality: bool
    status: str
    failure_reason: str | None = None
    netsuite_record_refs: dict | None = None
    correlation_id: str | None = None
    decided_by: StrFromUUID | None = None
    decided_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ResolutionGroupSummary(BaseModel):
    group_key: str
    root_cause: str
    action: str
    booking_vehicle: str
    count: int
    proposed_count: int
    approved_count: int
    total_amount: Decimal
    above_materiality_count: int


class ResolutionSummaryResponse(BaseModel):
    """Summary-first payload: one call renders the whole report header + groups.

    ``explained_rate`` = share of proposals whose action is not ``needs_human``
    (diagnostic: a falling rate signals upstream data problems, not just load).
    ``guard_skipped_count`` = results with no proposal at all — guard-skipped
    or never planned (a human-rejected proposal was still planned, so those
    results are NOT counted here).
    """

    run_id: str
    total_results: int
    matches_count: int
    match_rate: Decimal
    proposals_count: int
    explained_count: int
    explained_rate: Decimal
    guard_skipped_count: int
    variance_by_root_cause: dict[str, Decimal]
    groups: list[ResolutionGroupSummary]


class ResolutionGroupApprove(BaseModel):
    notes: str | None = None
    # Above-materiality items approve ONLY when explicitly ticked.
    included_above_materiality_ids: list[str] = []
    excluded_ids: list[str] = []


class ResolutionGroupApproveResult(BaseModel):
    run_id: str
    group_key: str
    approved_count: int
    skipped_count: int
    correlation_id: str


class ResolutionGroupRejectResult(BaseModel):
    run_id: str
    group_key: str
    rejected_count: int
    correlation_id: str


class ResolutionProposalOverride(BaseModel):
    action: ResolutionAction
    notes: str | None = None
