"""Bounded persistence inputs; none of these inputs can grant write approval."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, model_validator

Termination = Literal["done", "budget", "stall", "error"]
ProposalAction = Literal["sync_missing_order", "correct_amounts", "resolve_celigo_error"]
_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")


def _bounded_json(value):
    def visit(item, depth=0):
        if depth > 12:
            raise ValueError("JSON nesting exceeds the evidence limit")
        if isinstance(item, float):
            raise ValueError("Financial evidence must use decimal strings, never binary floats")
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Nonfinite evidence is invalid")
            return str(item)
        if item is None or isinstance(item, (bool, int, str)):
            return item
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            return {key: visit(val, depth + 1) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [visit(val, depth + 1) for val in item]
        raise ValueError("Evidence must contain JSON values")

    result = visit(value)
    if not isinstance(result, dict) or len(json.dumps(result).encode()) > 65536:
        raise ValueError("Evidence must be an object of at most 64 KiB")
    return result


BoundedJSON = Annotated[dict[str, JsonValue], BeforeValidator(_bounded_json)]
Identifier = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S(?:.*\S)?$")]
Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigCreate(InputModel):
    name: Identifier
    source_step_id: UUID
    netsuite_connection_id: UUID
    netsuite_account_id: Identifier
    subsidiary_id: Identifier
    record_type: Literal["salesorder"] = "salesorder"
    target_step_id: UUID | None = None
    mapping_json: BoundedJSON
    schedule_enabled: bool = False
    interval_minutes: int = Field(default=60, ge=5, le=10080, strict=True)
    max_api_calls: int = Field(default=100, ge=1, le=2000, strict=True)
    max_orders: int = Field(default=100, ge=1, le=10000, strict=True)
    deadline_seconds: int = Field(default=900, ge=30, le=3600, strict=True)


class ConfigControl(InputModel):
    enabled: bool
    schedule_enabled: bool


class RunCreate(InputModel):
    origin: Literal["chat", "manual", "schedule"] = "manual"
    evaluation_key: Identifier
    order_references: tuple[str, ...] = Field(default=(), max_length=200)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def exact_scope(self):
        if self.order_references:
            if self.window_start is not None or self.window_end is not None:
                raise ValueError("Choose exact orders or a time window")
            if any(len(ref) > 100 or not _REFERENCE.fullmatch(ref) for ref in self.order_references):
                raise ValueError("Full Framework order references are required")
            object.__setattr__(self, "order_references", tuple(sorted(set(self.order_references))))
        elif (
            self.window_start is None
            or self.window_end is None
            or not timedelta(0) < self.window_end - self.window_start <= timedelta(days=31)
        ):
            raise ValueError("An ordered window of at most 31 days is required")
        return self


class ProgressUpdate(InputModel):
    progress_json: BoundedJSON


class FindingReport(InputModel):
    order_reference: str = Field(pattern=r"^R[0-9]{9}(?:-[A-Z0-9]+)?$", max_length=100)
    report_json: BoundedJSON


class ProposalCreate(InputModel):
    source_record_id: Identifier
    order_reference: Identifier
    target_record_id: Identifier | None = None
    action: ProposalAction
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    evidence_fingerprint: Fingerprint
    observed_at: AwareDatetime
    before_json: BoundedJSON
    after_json: BoundedJSON
    evidence_json: BoundedJSON


class ProposalDecision(InputModel):
    decision: Literal["approve", "reject"]
    evidence_fingerprint: Fingerprint
    note: str | None = Field(default=None, max_length=2000)


class OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConfigOut(OutputModel):
    id: UUID
    tenant_id: UUID
    config_key: str
    name: str
    source_step_id: UUID
    netsuite_connection_id: UUID
    netsuite_account_id: str
    subsidiary_id: str
    record_type: str
    target_step_id: UUID | None
    mapping_json: dict
    enabled: bool
    schedule_enabled: bool
    interval_minutes: int
    max_api_calls: int
    max_orders: int
    deadline_seconds: int
    created_at: datetime


class RunOut(OutputModel):
    id: UUID
    tenant_id: UUID
    config_id: UUID
    work_key: str
    origin: str
    params_json: dict
    config_snapshot: dict
    status: str
    termination_reason: str | None
    max_api_calls: int
    max_orders: int
    api_calls_used: int
    orders_used: int
    deadline_at: datetime
    progress_json: dict
    created_at: datetime
    finished_at: datetime | None


class ProposalOut(OutputModel):
    id: UUID
    tenant_id: UUID
    config_id: UUID
    run_id: UUID
    work_key: str
    source_record_id: str
    order_reference: str
    target_record_id: str | None
    action: str
    currency: str
    netsuite_account_id: str
    subsidiary_id: str
    record_type: str
    evidence_fingerprint: str
    observed_at: datetime
    valid_until: datetime
    before_json: dict
    after_json: dict
    evidence_json: dict
    status: str
    decided_by: UUID | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime


class OperationOut(OutputModel):
    id: UUID
    tenant_id: UUID
    proposal_id: UUID
    work_key: str
    status: str
    attempted_at: datetime
    completed_at: datetime | None
    result_json: dict


class FindingOut(OutputModel):
    id: UUID
    run_id: UUID
    order_reference: str
    report_json: dict
    created_at: datetime
    updated_at: datetime


class ClaimedOperation(InputModel):
    """Returned only after the executing ledger row has been committed."""

    operation_id: UUID
    proposal_id: UUID
    work_key: str
    config_id: UUID
    action: ProposalAction
    currency: str
    netsuite_account_id: str
    subsidiary_id: str
    record_type: str
    target_record_id: str | None
    before_json: BoundedJSON
    after_json: BoundedJSON
