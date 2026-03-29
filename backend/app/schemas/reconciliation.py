"""Pydantic schemas for reconciliation engine."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums / Literals
# ---------------------------------------------------------------------------
MatchType = Literal["deterministic", "fuzzy", "unmatched", "exception"]
VarianceType = Literal["fees", "fx_rounding", "timing", "missing", "duplicate", "chargeback", "manual_adjustment"]
ResultStatus = Literal["pending", "auto_matched", "suggested", "approved", "rejected", "investigating", "locked"]
RunStatus = Literal["pending", "running", "completed", "failed", "closed"]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class ReconRunCreate(BaseModel):
    date_from: date
    date_to: date
    subsidiary_id: str | None = None
    payout_ids: list[str] | None = Field(default=None, description="Specific payout source_ids to reconcile")


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
    id: str
    run_id: str
    payout_id: str | None
    deposit_id: str | None
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
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReconRunResponse(BaseModel):
    id: str
    tenant_id: str
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
