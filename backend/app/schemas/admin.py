"""Pydantic schemas for the super admin API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WalletResponse(BaseModel):
    tenant_id: str
    stripe_customer_id: str | None = None
    stripe_subscription_item_id: str | None = None
    billing_period_start: datetime
    billing_period_end: datetime
    base_credits_remaining: int
    metered_credits_used: int
    last_synced_metered_credits: int

    model_config = {"from_attributes": True}


class AdminTenantResponse(BaseModel):
    id: str
    name: str
    slug: str
    plan: str
    is_active: bool
    created_at: datetime
    user_count: int = 0
    wallet: WalletResponse | None = None

    model_config = {"from_attributes": True}


class PlatformStatsResponse(BaseModel):
    active_tenants: int
    total_tenants: int
    total_users: int
    total_base_credits_remaining: int
    total_metered_credits_used: int


class ImpersonateResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    tenant_name: str


class WalletUpdateRequest(BaseModel):
    base_credits_remaining: int | None = Field(None, ge=0)
    stripe_customer_id: str | None = None
    stripe_subscription_item_id: str | None = None
