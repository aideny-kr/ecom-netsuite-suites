from datetime import datetime

from pydantic import BaseModel, Field


class ChatApiKeyCreate(BaseModel):
    name: str = Field(..., max_length=255)
    scopes: list[str] = Field(default_factory=lambda: ["chat"])
    rate_limit_per_minute: int = 60
    expires_at: datetime | None = None


class ChatApiKeyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    scopes: list[str] | None = None
    rate_limit_per_minute: int
    is_active: bool
    expires_at: datetime | None = None
    created_by: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatApiKeyCreated(BaseModel):
    """Returned once at creation time — includes the raw key."""

    id: str
    name: str
    key_prefix: str
    raw_key: str
    scopes: list[str] | None = None
    rate_limit_per_minute: int
    expires_at: datetime | None = None
    created_at: datetime
