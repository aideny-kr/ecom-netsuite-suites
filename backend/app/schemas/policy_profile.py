from datetime import datetime

from pydantic import BaseModel, Field


class PolicyProfileCreate(BaseModel):
    name: str = Field(..., max_length=255)
    is_active: bool = True
    read_only_mode: bool = True
    allowed_record_types: dict | None = None
    blocked_fields: dict | None = None
    max_rows_per_query: int = 1000
    require_row_limit: bool = True
    custom_rules: dict | None = None


class PolicyProfileUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    read_only_mode: bool | None = None
    allowed_record_types: dict | None = None
    blocked_fields: dict | None = None
    max_rows_per_query: int | None = None
    require_row_limit: bool | None = None
    custom_rules: dict | None = None


class PolicyProfileResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    is_active: bool
    read_only_mode: bool
    allowed_record_types: dict | None = None
    blocked_fields: dict | None = None
    max_rows_per_query: int
    require_row_limit: bool
    custom_rules: dict | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
