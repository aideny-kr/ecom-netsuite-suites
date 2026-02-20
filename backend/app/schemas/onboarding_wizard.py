from pydantic import BaseModel


class ChecklistItemResponse(BaseModel):
    step_key: str
    status: str
    completed_at: str | None = None
    completed_by: str | None = None
    metadata: dict | None = None


class ChecklistResponse(BaseModel):
    items: list[ChecklistItemResponse]
    all_completed: bool
    finalized_at: str | None = None


class StepValidationResponse(BaseModel):
    step_key: str
    valid: bool
    reason: str | None = None


class StepCompleteRequest(BaseModel):
    metadata: dict | None = None


class SetupPolicyRequest(BaseModel):
    read_only_mode: bool = True
    sensitivity_default: str = "financial"
    allowed_record_types: list[str] | None = None
    blocked_fields: list[str] | None = None
    tool_allowlist: list[str] | None = None
    max_rows_per_query: int = 1000
    require_row_limit: bool = True


class FinalizeResponse(BaseModel):
    success: bool
    completed_at: str | None = None


class AuditTrailEvent(BaseModel):
    id: str
    action: str
    created_at: str
    correlation_id: str | None = None
    actor_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    payload: dict | None = None


class AuditTrailResponse(BaseModel):
    events: list[AuditTrailEvent]
