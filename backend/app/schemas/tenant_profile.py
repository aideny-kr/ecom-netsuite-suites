from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# JSON columns can hold dicts or lists
JsonValue = dict[str, Any] | list[Any] | None


class TenantProfileCreate(BaseModel):
    industry: str | None = Field(None, max_length=100)
    team_size: str | None = Field(None, max_length=20)
    business_description: str | None = None
    netsuite_account_id: str | None = Field(None, max_length=100)
    chart_of_accounts: JsonValue = None
    subsidiaries: JsonValue = None
    item_types: JsonValue = None
    custom_segments: JsonValue = None
    fiscal_calendar: JsonValue = None
    suiteql_naming: JsonValue = None


class TenantProfileResponse(BaseModel):
    id: str
    tenant_id: str
    version: int
    status: str
    industry: str | None = None
    team_size: str | None = None
    business_description: str | None = None
    netsuite_account_id: str | None = None
    chart_of_accounts: JsonValue = None
    subsidiaries: JsonValue = None
    item_types: JsonValue = None
    custom_segments: JsonValue = None
    fiscal_calendar: JsonValue = None
    suiteql_naming: JsonValue = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TenantProfileConfirm(BaseModel):
    """Empty body — confirmation is an action, not a data update."""

    pass
