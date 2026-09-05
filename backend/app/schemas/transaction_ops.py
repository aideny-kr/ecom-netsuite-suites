"""Evidence contracts for transaction investigations, not write authorization.

Adapters must explicitly map fields into transaction-currency major units.
``subtotal`` excludes tax/shipping and precedes the positive ``discount``;
``tax`` includes shipping tax. Missing values remain unknown. A source's
``subsidiary_id`` is the configured NetSuite subsidiary mapping, never inferred
from a currency or address. Provenance/completeness fields are collector-owned.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _decimal(value, maximum=Decimal("1e24")):
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Amounts must be decimal strings, integers or Decimal, never binary floats")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Amount is not a decimal number") from None
    if not number.is_finite() or number.copy_abs() >= maximum or number.as_tuple().exponent < -12:
        raise ValueError("Amount is nonfinite or exceeds supported precision/range")
    return number


ExactDecimal = Annotated[Decimal, BeforeValidator(_decimal)]
ExactDelta = Annotated[Decimal, BeforeValidator(lambda value: _decimal(value, Decimal("2e24")))]
Identifier = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S(?:.*\S)?$")]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransactionLine(EvidenceModel):
    # Collector assigns a stable cross-system key; duplicate SKUs need distinct
    # order-line keys, not an arbitrary dict overwrite or fuzzy pairing.
    key: Identifier
    quantity: ExactDecimal
    net: ExactDecimal | None = None
    tax: ExactDecimal | None = None
    # Populated only by the explicit inventory identity profile. No serial numbers.
    inventory_unit_ids: tuple[Annotated[str, Field(pattern=r"^[1-9][0-9]{0,29}$")], ...] = Field(
        default=(), max_length=500
    )
    sku: Identifier | None = None

    @model_validator(mode="after")
    def unique_inventory(self):
        if len(set(self.inventory_unit_ids)) != len(self.inventory_unit_ids):
            raise ValueError("Inventory-unit ownership must be unique")
        return self


class TransactionTax(EvidenceModel):
    # One taxable event, not a sum of separately rounded line taxes. Collectors
    # must retain individual components when the provider rounds per line.
    key: Identifier
    calculation: Literal["statutory_rate", "reported_allocation"] = "statutory_rate"
    # A collector may bind several separately validated source components to
    # one legacy destination tax allocation. Their original keys/rates remain.
    allocation_key: Identifier | None = None
    basis: ExactDecimal | None = None
    rate: ExactDecimal | None = None  # fraction (0.20), not a percentage (20)
    amount: ExactDecimal | None = None
    rounding: Literal["half_up", "half_even"] | None = None
    # Included VAT is calculated on the original gross basis. Recalculating
    # from an already rounded net amount can change a one-cent tax result.
    included_gross_basis: ExactDecimal | None = None
    included_rate_total: ExactDecimal | None = None

    @model_validator(mode="after")
    def included_calculation(self):
        if self.allocation_key is not None and not self.key.startswith(self.allocation_key + ":source_rate:"):
            raise ValueError("An allocation must retain the original taxable-event identity")
        if self.calculation == "reported_allocation" and any(
            value is not None
            for value in (
                self.rate,
                self.rounding,
                self.included_gross_basis,
                self.included_rate_total,
                self.allocation_key,
            )
        ):
            raise ValueError("Reported allocations cannot claim a statutory calculation or further aggregation")
        if (self.included_gross_basis is None) != (self.included_rate_total is None):
            raise ValueError("Included tax needs both gross basis and total included rate")
        if self.included_gross_basis is not None and (
            self.included_gross_basis < 0 or self.rate is None or self.rate < 0 or self.included_rate_total < self.rate
        ):
            raise ValueError("Included tax basis/rates must be nonnegative and total must include this rate")
        return self


class TransactionSnapshot(EvidenceModel):
    system: Identifier
    account_id: Identifier
    record_id: Identifier
    record_type: Identifier
    order_reference: Identifier
    subsidiary_id: Identifier | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    currency_minor_unit: int | None = Field(default=None, ge=0, le=6, strict=True)
    amount_basis: Literal["transaction", "base", "unknown"] = "unknown"
    status: Literal["draft", "confirmed", "fulfilled", "cancelled", "refunded", "unknown"] = "unknown"
    updated_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    authoritative: bool = False
    total: ExactDecimal | None = None
    subtotal: ExactDecimal | None = None
    tax: ExactDecimal | None = None
    shipping: ExactDecimal | None = None
    shipping_tax: ExactDecimal | None = None
    discount: ExactDecimal | None = None
    lines: tuple[TransactionLine, ...] = Field(default=(), max_length=2000)
    tax_details: tuple[TransactionTax, ...] = Field(default=(), max_length=2000)
    lines_complete: bool = False
    tax_complete: bool = False

    @model_validator(mode="after")
    def unique_detail_keys(self):
        if self.system != "netsuite" and any(tax.calculation != "statutory_rate" for tax in self.tax_details):
            raise ValueError("Source taxes require statutory calculation evidence")
        if self.system == "netsuite" and any(tax.allocation_key is not None for tax in self.tax_details):
            raise ValueError("Only source components can request a destination allocation")
        for values in (self.lines, self.tax_details):
            if len({item.key for item in values}) != len(values):
                raise ValueError("Detail keys must be unique; ambiguous lines cannot be collapsed")
        return self


class CeligoTransactionErrorScope(EvidenceModel):
    connection_id: Identifier
    flow_id: Identifier
    step_id: Identifier
    target_account_id: Identifier
    target_subsidiary_id: Identifier
    target_record_type: Identifier
    operation: Literal["create", "update", "upsert", "unknown"] = "unknown"
    kind: Literal["duplicate_transaction", "other", "unknown"] = "unknown"


class TransactionLookup(EvidenceModel):
    source_system: Identifier
    source_account_id: Identifier
    source_record_id: Identifier
    order_reference: Identifier
    target_account_id: Identifier
    target_subsidiary_id: Identifier
    target_record_type: Identifier
    # Complete means exact identity lookup with no date/status filter hiding a
    # counterpart, all pages consumed and no upstream truncation/error.
    complete: bool = False
    authoritative: bool = False
    observed_at: AwareDatetime
    celigo_error_id: Identifier | None = None
    error_order_reference: Identifier | None = None
    error_observed_at: AwareDatetime | None = None
    error_is_open: bool = False
    error_scope: CeligoTransactionErrorScope | None = None


class TransactionFinding(EvidenceModel):
    code: str
    reason: str


class TransactionDifference(EvidenceModel):
    field: str
    source: ExactDecimal
    target: ExactDecimal
    delta: ExactDelta  # subtraction can have twice an operand's magnitude


class TransactionComparison(EvidenceModel):
    recommended_action: Literal[
        "gather_evidence",
        "human_review",
        "propose_missing_sync",
        "propose_amount_correction",
        "propose_false_alarm_resolution",
        "no_action",
    ]
    requires_human_approval: Literal[True] = True
    currency: str | None
    evidence_fingerprint: str
    findings: tuple[TransactionFinding, ...] = ()
    differences: tuple[TransactionDifference, ...] = ()
