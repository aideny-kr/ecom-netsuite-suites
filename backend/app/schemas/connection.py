from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConnectionCreate(BaseModel):
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
    created_at: datetime
    created_by: str | None = None

    model_config = {"from_attributes": True}


class ConnectionTestResponse(BaseModel):
    connection_id: str
    status: str
    message: str
