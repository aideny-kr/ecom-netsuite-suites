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

# The composing LLM consistently emits common-sense `type` names — `text` for a
# narrative, `data` for a table — and sometimes omits the required
# `narrative.markdown`. Those fail the discriminated-union validation, the agent
# retries 2-4x, and the turn times out. Tolerate the aliases at the validation
# boundary (cheap, deterministic) so a good composition is not thrown away over a
# naming nit. Canonical types pass through untouched; truly unknown types still raise.
_TYPE_ALIASES = {"text": "narrative", "data": "table"}
_NARRATIVE_BODY_ALIASES = ("text", "content", "body")


def _normalize_section(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    if out.get("type") in _TYPE_ALIASES:
        out["type"] = _TYPE_ALIASES[out["type"]]
    # narrative requires `markdown`; tolerate the body arriving under text/content/body
    if out.get("type") == "narrative" and "markdown" not in out:
        for alias in _NARRATIVE_BODY_ALIASES:
            val = out.get(alias)
            if isinstance(val, str):
                out["markdown"] = val
                break
    return out


def normalize_sections(raw: list[dict]) -> list[dict]:
    """Map the LLM's common section-type aliases onto the canonical schema.

    Returns a NEW list of normalized dicts; the input is not mutated. This must be
    applied at every boundary that reads section ``type`` from raw dicts. There are
    two: ``assemble_spec`` (the production chat-tool render path, which never builds
    ComposeRequest) and ``ComposeRequest`` validation (API / tests). A boundary that
    validates the normalized form but then reads the un-normalized input would let a
    `text`/`data` section pass validation and be dropped silently by the renderer.
    """
    return [_normalize_section(s) for s in raw]


def parse_sections(raw: list[dict]) -> list:
    return _SECTIONS_ADAPTER.validate_python(normalize_sections(raw))


class ComposeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    # The API/validation boundary (NOT the chat-tool path — that goes
    # report_export.execute -> compose_report -> assemble_spec on raw dicts and is
    # canonicalized there). The validator normalizes aliases + enforces the
    # discriminated union so an unknown `type` is rejected with a 422 before any work,
    # and stores the normalized dicts back so any consumer of this model also sees
    # canonical `narrative`/`table` types rather than the LLM's `text`/`data` aliases.
    sections: list[dict] = Field(min_length=1)

    @field_validator("sections")
    @classmethod
    def _validate_section_union(cls, v: list[dict]) -> list[dict]:
        normalized = normalize_sections(v)
        _SECTIONS_ADAPTER.validate_python(normalized)  # raises ValidationError on unknown/invalid type
        return normalized


class ReportResponse(BaseModel):
    id: str
    title: str
    status: str
    version: int
    created_at: datetime
    model_config = {"from_attributes": True}
