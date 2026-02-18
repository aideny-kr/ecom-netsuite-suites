from datetime import datetime

from pydantic import BaseModel


class PromptTemplateResponse(BaseModel):
    id: str
    tenant_id: str
    version: int
    profile_id: str
    policy_id: str | None = None
    template_text: str
    sections: dict | None = None
    is_active: bool
    generated_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PromptTemplatePreview(BaseModel):
    template_text: str
    sections: dict
