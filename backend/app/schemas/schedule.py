import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# Dangerous patterns that should never appear in schedule parameters
_DANGEROUS_PATTERNS = re.compile(
    r"(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+SET|ALTER\s+TABLE|TRUNCATE|"
    r"EXEC\s*\(|xp_cmdshell|UNION\s+SELECT|;\s*SELECT|--\s|/\*|"
    r"</instructions>|<system>|</system>|</prompt>|<context>|<tool_call>|"
    r"sleep\s*\(|benchmark\s*\(|pg_sleep|waitfor\s+delay)",
    re.IGNORECASE
)

ALLOWED_SCHEDULE_TYPES = frozenset({"sync", "report", "recon"})
MAX_PARAM_DEPTH = 3
MAX_PARAM_STRING_LENGTH = 1000


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
    name: str = Field(min_length=1, max_length=255)
    schedule_type: str = Field(min_length=1, max_length=100)
    cron_expression: Optional[str] = Field(default=None, max_length=100, pattern=r"^[\d\s\*\/\-\,\?LW\#]+$")
    parameters: Optional[dict] = None

    @field_validator("schedule_type")
    @classmethod
    def validate_schedule_type(cls, v: str) -> str:
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


class ScheduleResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    schedule_type: str
    cron_expression: Optional[str]
    is_active: bool
    parameters: Optional[dict]

    model_config = {"from_attributes": True}
