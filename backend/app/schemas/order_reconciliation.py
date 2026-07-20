"""Schemas for order-level reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ChargeRecord(BaseModel):
    id: str
    source_id: str  # Stripe charge ID (ch_xxx)
    payout_line_id: str
    amount: Decimal  # Gross charge amount
    fee: Decimal
    net: Decimal
    currency: str
    charge_date: date
    description: str | None = None
    order_reference: str | None = None  # Extracted R\d{9}
    customer_email: str | None = None


class NSPaymentRecord(BaseModel):
    id: str
    netsuite_internal_id: str
    amount: Decimal
    currency: str
    transaction_date: date
    record_type: str
    memo: str | None = None
    customer_name: str | None = None
    order_reference: str | None = None  # From createdfrom sales order


class OrderMatchCandidate(BaseModel):
    charge: ChargeRecord
    deposit: NSPaymentRecord | None
    match_type: str  # deterministic, fuzzy, unmatched
    confidence: Decimal
    variance_amount: Decimal = Decimal("0")
    variance_type: str | None = None
    variance_explanation: str | None = None
    match_rule: str | None = None
    # Populated when tier-1 resolved a same-ref group: several deposits and/or
    # charges shared an order_reference (e.g. an original posting plus a
    # correction/reversal, or a genuine multi-charge split order). Holds the
    # ids of deposits left over after set-to-set pairing within the group
    # (never assigned to any charge) — the SAME list on every result of that
    # group. Empty when there was no same-ref collision.
    same_ref_deposit_ids: list[str] = Field(default_factory=list)
    # True when tier-1 could not confidently pair this charge to a deposit by
    # exact amount (a deposit-count surplus for its amount bucket, or no
    # amount-exact deposit at all in its same-ref group) and instead picked
    # the nearest-transaction-date remaining deposit. An ambiguous pick must
    # never auto-match: OrderMatchingEngine caps its confidence below the
    # auto_match threshold and OrderReconJob routes it to needs_review.
    ambiguous_same_ref: bool = False
