from typing import Literal

from pydantic import BaseModel


class MetricCreate(BaseModel):
    key: str
    display_name: str
    definition: str
    unit: str
    source_kind: str
    blessed_spec: dict | None = None
    expression: str | None = None
    depends_on: list[str] | None = None
    params_schema: dict | None = None
    dimensions: dict | None = None
    synonyms: list[str] | None = None


class MetricUpdate(BaseModel):
    """All fields are optional — only provided fields are applied to the existing row."""

    display_name: str | None = None
    definition: str | None = None
    unit: str | None = None
    blessed_spec: dict | None = None
    expression: str | None = None
    depends_on: list[str] | None = None
    params_schema: dict | None = None
    dimensions: dict | None = None
    synonyms: list[str] | None = None
    # Constrain to the known lifecycle states so a PUT can't persist a garbage status
    # (an unconstrained str would let "garbage" through → 200 + invalid row).
    status: Literal["active", "draft", "needs_review", "deprecated"] | None = None


class MetricResponse(BaseModel):
    id: str
    key: str
    display_name: str
    unit: str
    source_kind: str
    status: str
    version: int
