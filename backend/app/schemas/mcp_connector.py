from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


def _validate_service_account_json(v: dict) -> dict:
    """Reject service-account dicts that are empty or missing required auth fields.

    Fails at the API boundary with a 422 so the service layer never has to handle
    obviously-invalid credentials — preventing the empty-dict → ValueError → 500
    failure mode.
    """
    if not v:
        raise ValueError("service_account_json must not be empty")
    missing = [f for f in ("client_email", "private_key") if f not in v]
    if missing:
        raise ValueError(f"service_account_json missing required fields: {missing}")
    return v


ServiceAccountJson = Annotated[dict, AfterValidator(_validate_service_account_json)]


class McpConnectorCreate(BaseModel):
    provider: str = Field(pattern=r"^(netsuite_mcp|shopify_mcp|stripe_mcp|custom)$")
    label: str = Field(min_length=1, max_length=255)
    server_url: str = Field(default="", max_length=1024)
    auth_type: str = Field(default="none", pattern=r"^(bearer|api_key|none|oauth2)$")
    credentials: dict | None = None


class McpConnectorResponse(BaseModel):
    id: str
    tenant_id: str
    provider: str
    label: str
    server_url: str
    auth_type: str
    status: str
    discovered_tools: list[dict] | None = None
    is_enabled: bool
    encryption_key_version: int
    metadata_json: dict | None = None
    last_health_check_at: datetime | None = None
    error_reason: str | None = None
    created_at: datetime
    created_by: str | None = None

    model_config = {"from_attributes": True}


class McpConnectorTestResponse(BaseModel):
    connector_id: str
    status: str
    message: str
    discovered_tools: list[dict] | None = None


class BigQueryTestRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=255)
    service_account_json: ServiceAccountJson
    location: str | None = Field(None, max_length=50)


class BigQueryTestResponse(BaseModel):
    valid: bool
    datasets: list[str] = Field(default_factory=list)
    error: str | None = None


class BigQueryConnectorCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=255)
    service_account_json: ServiceAccountJson
    default_dataset: str | None = None
    location: str | None = Field(None, max_length=50)


class BigQueryTableSelection(BaseModel):
    selected_tables: dict[str, list[str]]  # dataset_id -> [table_ids]


class SheetsTestRequest(BaseModel):
    service_account_json: ServiceAccountJson


class SheetsTestResponse(BaseModel):
    valid: bool
    error: str | None = None


class SheetsConnectorCreate(BaseModel):
    service_account_json: ServiceAccountJson
    label: str = Field(default="Google Sheets", min_length=1, max_length=255)
