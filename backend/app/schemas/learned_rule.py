"""Schemas for the tenant learned-rules management API."""

from datetime import datetime

from pydantic import BaseModel, Field


class LearnedRuleResponse(BaseModel):
    id: str
    tenant_id: str
    rule_category: str | None
    rule_description: str
    is_active: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LearnedRuleCreate(BaseModel):
    rule_description: str = Field(min_length=1, max_length=4000)
    rule_category: str | None = Field(default=None, max_length=50)


class LearnedRuleUpdate(BaseModel):
    rule_description: str | None = Field(default=None, min_length=1, max_length=4000)
    rule_category: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None
