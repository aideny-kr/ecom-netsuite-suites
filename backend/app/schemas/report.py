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
    # Optional human title for the rendered chart ("Cash Balance Trend"). A TITLE only,
    # never data — numbers still flow exclusively from the frozen payload.
    label: str | None = None


class TableSection(BaseModel):
    type: Literal["table"]
    result_id: str
    select: list[str] | None = None
    # Optional human title; also titles the table's auto-injected chart.
    label: str | None = None


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
    # Guard the membership test: a malformed `type` (list/dict, unhashable) must flow
    # to pydantic as a clean ValidationError, NOT raise TypeError here.
    section_type = out.get("type")
    if isinstance(section_type, str) and section_type in _TYPE_ALIASES:
        out["type"] = _TYPE_ALIASES[section_type]
    # narrative requires a STRING `markdown`; tolerate the body arriving under
    # text/content/body, including when `markdown` is present but not a string (null).
    if out.get("type") == "narrative" and not isinstance(out.get("markdown"), str):
        for alias in _NARRATIVE_BODY_ALIASES:
            val = out.get(alias)
            if isinstance(val, str):
                out["markdown"] = val
                break
    return out


def normalize_sections(raw: list[dict]) -> list[dict]:
    """Map the LLM's common section-type aliases onto the canonical schema.

    Returns a NEW list of normalized dicts; the input is not mutated. Apply at every
    boundary that reads section ``type`` from raw dicts — see
    ``normalize_and_validate_sections``.
    """
    return [_normalize_section(s) for s in raw]


def normalize_and_validate_sections(raw: list[dict]) -> list[dict]:
    """Normalize aliases ONCE, validate the discriminated union, return normalized DICTS.

    The single entry point for the two raw-dict boundaries — ``assemble_spec`` (the
    production chat-tool render path, which never builds ComposeRequest) and
    ``ComposeRequest`` validation (API/tests). Both need canonical dicts to iterate AND
    a loud raise on a truly-unknown type; routing both through here avoids
    re-implementing validation and double-normalizing.
    """
    normalized = normalize_sections(raw)
    _SECTIONS_ADAPTER.validate_python(normalized)  # raises ValidationError on unknown/invalid type
    return normalized


def parse_sections(raw: list[dict]) -> list:
    """Validate raw sections and return the parsed pydantic model objects."""
    return _SECTIONS_ADAPTER.validate_python(normalize_sections(raw))


class ComposeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    # The API/validation boundary (NOT the chat-tool path — that goes
    # report_export.execute -> compose_report -> assemble_spec on raw dicts and is
    # canonicalized there via normalize_and_validate_sections). The validator
    # canonicalizes aliases + enforces the discriminated union so an unknown `type` is
    # rejected with a 422 before any work, and stores the normalized dicts back so any
    # consumer of this model sees canonical `narrative`/`table` types, not `text`/`data`.
    sections: list[dict] = Field(min_length=1)

    @field_validator("sections")
    @classmethod
    def _validate_section_union(cls, v: list[dict]) -> list[dict]:
        return normalize_and_validate_sections(v)


class ReportResponse(BaseModel):
    id: str
    title: str
    status: str
    version: int
    created_at: datetime
    # Slice A: whether a refresh recipe was captured (the FE shows Refresh iff true —
    # Slice B). ONLY the boolean is exposed, never the raw recipe (params embed SQL).
    has_recipe: bool = False
    # Slice B: the "data as of" stamp source; None = never refreshed (compose-time data).
    last_refreshed_at: datetime | None = None
    # Slice C: the FE derives the interval selector + staleness/paused banners from
    # these — failure_count > 0 = "auto-refresh failing" banner; paused_at set =
    # paused banner + one-click Resume.
    auto_refresh: str = "daily"
    refresh_failure_count: int = 0
    auto_refresh_paused_at: datetime | None = None
    model_config = {"from_attributes": True}


class ReportSettingsUpdate(BaseModel):
    """PATCH /reports/{id}/settings — the interval is the only mutable setting; the
    Literal is the validity gate (the column is convention-only, like reports.status)."""

    auto_refresh: Literal["off", "hourly", "daily"]


class ReportVersionResponse(BaseModel):
    """One entry in the version picker (Slice B). Immutable snapshots; `is_current`
    marks the version the stable /view URL serves."""

    version: int
    created_at: datetime
    created_by: str | None = None
    pinned: bool = False
    is_current: bool = False
