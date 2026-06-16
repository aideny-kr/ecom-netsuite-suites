"""Schemas for the tenant memory graph management API.

`id` / `tenant_id` (and every other UUID) are typed `str` in responses because
`from_attributes` does NOT coerce a UUID column to str — the router's explicit
`_*_to_response()` helpers do the `str()`. `confidence` is a DB Numeric → it is
coerced to `float` by those same helpers (Decimal is not JSON-native here).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemoryConceptResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    summary: str
    concept_type: str | None
    review_state: str
    confidence: float | None
    confirmed_by: str | None
    use_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MemoryEdgeResponse(BaseModel):
    id: str
    tenant_id: str
    source_concept_id: str
    target_concept_id: str
    relation: str
    review_state: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MemoryLinkResponse(BaseModel):
    id: str
    tenant_id: str
    concept_id: str
    source_table: str
    source_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MemoryGraphResponse(BaseModel):
    concepts: list[MemoryConceptResponse]
    edges: list[MemoryEdgeResponse]


class MemoryConceptDetail(MemoryConceptResponse):
    links: list[MemoryLinkResponse]


class MemoryConceptUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    concept_type: str | None = Field(default=None, max_length=50)
    review_state: Literal["pending", "confirmed", "rejected"] | None = None
