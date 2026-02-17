from datetime import datetime

from pydantic import BaseModel, Field


class McpConnectorCreate(BaseModel):
    provider: str = Field(pattern=r"^(netsuite_mcp|shopify_mcp|custom)$")
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
    created_at: datetime
    created_by: str | None = None

    model_config = {"from_attributes": True}


class McpConnectorTestResponse(BaseModel):
    connector_id: str
    status: str
    message: str
    discovered_tools: list[dict] | None = None
