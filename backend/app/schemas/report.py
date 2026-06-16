from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator


class HeadingSection(BaseModel):
    type: Literal["heading"]
    level: int = Field(default=2, ge=1, le=3)
    text: str


class NarrativeSection(BaseModel):
    type: Literal["narrative"]
    markdown: str  # may contain {{result:<id>.<field>}} / {{metric:<id>}} placeholders


class MetricHeadlineSection(BaseModel):
    type: Literal["metric_headline"]
    result_id: str
    label: str | None = None


class ChartSection(BaseModel):
    type: Literal["chart"]
    result_id: str
    chart_type: Literal["bar", "line", "pie", "area", "scatter", "donut", "histogram"] | None = None


class TableSection(BaseModel):
    type: Literal["table"]
    result_id: str
    select: list[str] | None = None


class DividerSection(BaseModel):
    type: Literal["divider"]


ComposeSection = Annotated[
    Union[HeadingSection, NarrativeSection, MetricHeadlineSection, ChartSection, TableSection, DividerSection],
    Field(discriminator="type"),
]

_SECTIONS_ADAPTER = TypeAdapter(list[ComposeSection])


def parse_sections(raw: list[dict]) -> list:
    return _SECTIONS_ADAPTER.validate_python(raw)


class ComposeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    # Kept as raw dicts so downstream (compose_report / assemble_spec) consumes them
    # as plain dicts, but the validator enforces the discriminated union at construction
    # so an unknown `type` is rejected with a 422 before any work happens.
    sections: list[dict] = Field(min_length=1)

    @field_validator("sections")
    @classmethod
    def _validate_section_union(cls, v: list[dict]) -> list[dict]:
        parse_sections(v)  # raises ValidationError on unknown/invalid section type
        return v


class ReportResponse(BaseModel):
    id: str
    title: str
    status: str
    version: int
    created_at: datetime
    model_config = {"from_attributes": True}
