from datetime import datetime

from pydantic import BaseModel, field_validator

from app.services.chat.llm_adapter import VALID_MODELS, VALID_PROVIDERS


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
    ai_provider: str | None = None
    ai_model: str | None = None
    ai_api_key_set: bool = False

    model_config = {"from_attributes": True}


class TenantConfigUpdate(BaseModel):
    subsidiaries: dict | None = None
    account_mappings: dict | None = None
    posting_mode: str | None = None
    posting_batch_size: int | None = None
    posting_attach_evidence: bool | None = None
    netsuite_account_id: str | None = None
    ai_provider: str | None = None
    ai_model: str | None = None
    ai_api_key: str | None = None

    @field_validator("ai_provider")
    @classmethod
    def validate_provider(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_PROVIDERS:
            raise ValueError(f"Invalid provider. Must be one of: {', '.join(sorted(VALID_PROVIDERS))}")
        return v

    @field_validator("ai_model")
    @classmethod
    def validate_model(cls, v: str | None) -> str | None:
        if v is not None:
            all_models = [m for models in VALID_MODELS.values() for m in models]
            if v not in all_models:
                raise ValueError(f"Invalid model: {v}")
        return v


class AiKeyTestRequest(BaseModel):
    provider: str
    api_key: str
    model: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v not in VALID_PROVIDERS:
            raise ValueError(f"Invalid provider. Must be one of: {', '.join(sorted(VALID_PROVIDERS))}")
        return v


class AiKeyTestResponse(BaseModel):
    valid: bool
    error: str | None = None
