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


class MetricResponse(BaseModel):
    id: str
    key: str
    display_name: str
    unit: str
    source_kind: str
    status: str
    version: int
