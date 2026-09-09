import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Dangerous patterns that should never appear in schedule parameters
_DANGEROUS_PATTERNS = re.compile(
    r"(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+SET|ALTER\s+TABLE|TRUNCATE|"
    r"EXEC\s*\(|xp_cmdshell|UNION\s+SELECT|;\s*SELECT|--\s|/\*|"
    r"</instructions>|<system>|</system>|</prompt>|<context>|<tool_call>|"
    r"sleep\s*\(|benchmark\s*\(|pg_sleep|waitfor\s+delay)",
    re.IGNORECASE,
)

# "job" (Slice 2, spec §B1) is a Scheduled Job compiled from `instruction` via
# app.services.jobs.compiler — see ScheduleCreate's model_validator below for
# how it differs from the three pre-existing opaque-parameter-bag types.
ALLOWED_SCHEDULE_TYPES = frozenset({"sync", "report", "recon", "job"})
ALLOWED_CATCH_UP = frozenset({"once", "skip"})
MAX_PARAM_DEPTH = 3
MAX_PARAM_STRING_LENGTH = 1000
MAX_INSTRUCTION_LENGTH = 4000


def _validate_param_value(value, depth=0):
    """Recursively validate parameter values."""
    if depth > MAX_PARAM_DEPTH:
        raise ValueError("Parameter nesting too deep (max 3 levels)")

    if isinstance(value, str):
        if len(value) > MAX_PARAM_STRING_LENGTH:
            raise ValueError(f"Parameter string too long (max {MAX_PARAM_STRING_LENGTH} chars)")
        if _DANGEROUS_PATTERNS.search(value):
            raise ValueError("Parameter contains disallowed SQL or injection pattern")
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("Parameter keys must be strings")
            _validate_param_value(v, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_param_value(item, depth + 1)
    elif not isinstance(value, (int, float, bool, type(None))):
        raise ValueError(f"Unsupported parameter type: {type(value).__name__}")


class ScheduleCreate(BaseModel):
    """Two shapes in one model, disambiguated by `instruction` (spec §B5):

    - **Scheduled Job** (`instruction` given): `{name?, instruction,
      cron_expression?, timezone?, delivery?}` -> the API compiles it (see
      `app.api.v1.schedules.create_schedule`). `schedule_type` is ignored if
      given — the created row is always `schedule_type="job"`.
    - **Legacy schedule** (`instruction` absent, pre-Slice-2 behaviour
      unchanged): `{name, schedule_type, cron_expression?, parameters?}`,
      `schedule_type` one of `sync|report|recon`.
    """

    name: Optional[str] = Field(default=None, max_length=255)
    schedule_type: Optional[str] = Field(default=None, min_length=1, max_length=100)
    cron_expression: Optional[str] = Field(default=None, max_length=100, pattern=r"^[\d\s\*\/\-\,\?LW\#]+$")
    parameters: Optional[dict] = None
    instruction: Optional[str] = Field(default=None, min_length=1, max_length=MAX_INSTRUCTION_LENGTH)
    timezone: Optional[str] = Field(default=None, max_length=64)
    delivery: Optional[dict] = None

    @field_validator("schedule_type")
    @classmethod
    def validate_schedule_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_SCHEDULE_TYPES:
            raise ValueError(f"schedule_type must be one of {sorted(ALLOWED_SCHEDULE_TYPES)}, got '{v}'")
        return v

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        _validate_param_value(v, depth=0)
        return v

    @model_validator(mode="after")
    def check_instruction_or_type(self) -> "ScheduleCreate":
        if not self.instruction and not self.schedule_type:
            raise ValueError(
                "Either 'instruction' (a Scheduled Job) or 'schedule_type' (a legacy schedule) is required"
            )
        return self


class ScheduleUpdate(BaseModel):
    """`PATCH /schedules/{id}` body (spec §B5): an `instruction` edit
    recompiles into `pending_plan_json` (or, for a schedule with no approved
    plan yet, directly into `plan_json` — see the endpoint); every other
    field applies directly, no approval needed."""

    instruction: Optional[str] = Field(default=None, min_length=1, max_length=MAX_INSTRUCTION_LENGTH)
    cron_expression: Optional[str] = Field(default=None, max_length=100, pattern=r"^[\d\s\*\/\-\,\?LW\#]+$")
    timezone: Optional[str] = Field(default=None, max_length=64)
    delivery: Optional[dict] = None
    budget: Optional[dict] = None
    catch_up: Optional[str] = None
    name: Optional[str] = Field(default=None, max_length=255)
    # Task 6 (frontend detail page, spec §B5/§B6): the pending-change panel's
    # "Discard" button — drops a recompiled `pending_plan_json` a person
    # doesn't want, leaving the live `plan_json` (still what actually runs)
    # untouched. `True` clears it; `False`/absent is a no-op (never clears
    # implicitly on an unrelated field edit).
    discard_pending: Optional[bool] = None

    @field_validator("catch_up")
    @classmethod
    def validate_catch_up(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_CATCH_UP:
            raise ValueError(f"catch_up must be one of {sorted(ALLOWED_CATCH_UP)}, got '{v}'")
        return v


class ScheduleRunRequest(BaseModel):
    use_pending: bool = False


class DiffLineOut(BaseModel):
    kind: str  # "add" | "del" | "ctx"
    step: Optional[int]
    text: str


class ScheduleResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    schedule_type: str
    cron_expression: Optional[str]
    is_active: bool
    parameters: Optional[dict]
    instruction: Optional[str] = None
    plan_status: Optional[str] = None
    plan_version: int = 0
    timezone: str = "UTC"
    delivery_json: Optional[dict] = None
    budget_json: Optional[dict] = None
    catch_up: str = "once"
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
    next_run_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    pause_reason: Optional[str] = None
    kinds: list[str] = Field(default_factory=list)
    summary_line: Optional[str] = None
    has_pending_plan: bool = False
    # Task 5 residual (spec §B6, mock state one): the list page's Job column
    # sub-line and Last run cell duration. Computed server-side from `jobs`
    # (`schedule_service.schedule_run_stats`) -- never stored on the row
    # itself, so these are `None`/`0` unless the list endpoint fills them in.
    last_run_duration_seconds: Optional[float] = None
    runs_last_7_days: int = 0
    owner_name: Optional[str] = None
    created_via: Optional[str] = None

    model_config = {"from_attributes": True}


class ScheduleListResponse(BaseModel):
    """`GET /schedules`'s response shape (Task 5 residual, spec §B6): the
    tenant's own rows PLUS the list page's "Last 7 days" tile's tenant-wide
    totals. The tile is TENANT-scoped, not per-schedule, so it cannot live on
    any one `ScheduleResponse` row — it is computed by ONE aggregate query
    (`schedule_service.tenant_run_totals_7d`), not N+1, and returned
    alongside the list rather than via a second round-trip."""

    schedules: list[ScheduleResponse]
    runs_last_7_days_total: int = 0
    runs_last_7_days_failed: int = 0


class ScheduleDetailResponse(ScheduleResponse):
    plan_json: Optional[dict] = None
    pending_plan_json: Optional[dict] = None
    pending_plan_reason: Optional[str] = None
    pending_plan_diff: list[DiffLineOut] = Field(default_factory=list)
    owner_id: Optional[str] = None


class ScheduleRunResponse(BaseModel):
    """`POST /schedules/{id}/run`'s response (Task 5 residual: this now
    enqueues via Celery instead of running inline — see the endpoint's own
    docstring). `jobs_id` is real and present immediately (the endpoint
    creates that row itself before dispatching); `reason`/`outputs` are
    `None`/`{}` at enqueue time — the run has not necessarily happened yet —
    and are populated once it has, readable via `GET /schedules/{id}/runs`."""

    jobs_id: Optional[str] = None
    status: str = "queued"
    reason: Optional[str] = None
    outputs: dict = Field(default_factory=dict)


class ScheduleRunItem(BaseModel):
    id: str
    status: str
    reason: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    correlation_id: Optional[str] = None
    plan_version: Optional[int] = None
    attempt: Optional[int] = None
    outputs: dict = Field(default_factory=dict)
    detail: Optional[str] = None
