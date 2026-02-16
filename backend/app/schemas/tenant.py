from datetime import datetime
from pydantic import BaseModel


class TenantResponse(BaseModel):
    id: str
    name: str
    slug: str
    plan: str
    plan_expires_at: datetime | None = None
    is_active: bool

    model_config = {"from_attributes": True}


class TenantUpdate(BaseModel):
    name: str | None = None


class TenantConfigResponse(BaseModel):
    id: str
    tenant_id: str
    subsidiaries: dict | None = None
    account_mappings: dict | None = None
    posting_mode: str
    posting_batch_size: int
    posting_attach_evidence: bool
    netsuite_account_id: str | None = None

    model_config = {"from_attributes": True}


class TenantConfigUpdate(BaseModel):
    subsidiaries: dict | None = None
    account_mappings: dict | None = None
    posting_mode: str | None = None
    posting_batch_size: int | None = None
    posting_attach_evidence: bool | None = None
    netsuite_account_id: str | None = None
