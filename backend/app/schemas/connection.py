from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConnectionCreate(BaseModel):
    # celigo is deliberately excluded: it is reachable only through
    # connect_celigo (app/api/v1/connector_status.py), which builds
    # Connection(...) directly and enforces the feature flag,
    # verify-token-before-write, and reconnect semantics this generic schema
    # knows nothing about. Widening this pattern to admit "celigo" would let
    # POST /connections create a Celigo row that bypasses every one of those
    # guards -- see tests/schemas/test_celigo_provider_schemas.py.
    provider: str = Field(pattern=r"^(shopify|stripe|netsuite)$")
    label: str = Field(min_length=1, max_length=255)
    credentials: dict


class ConnectionUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=255)
    auth_type: Literal["oauth2", "oauth1_tba"] | None = None


class ConnectionResponse(BaseModel):
    id: str
    tenant_id: str
    provider: str
    label: str
    status: str
    auth_type: str | None = None
    encryption_key_version: int
    metadata_json: dict | None = None
    last_health_check_at: datetime | None = None
    error_reason: str | None = None
    created_at: datetime
    created_by: str | None = None

    model_config = {"from_attributes": True}


class ConnectionTestResponse(BaseModel):
    connection_id: str
    status: str
    message: str
