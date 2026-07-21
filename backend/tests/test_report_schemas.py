import pytest
from pydantic import ValidationError

from app.schemas.report import ComposeRequest, parse_sections


def test_parse_valid_sections():
    req = ComposeRequest(
        title="Q2",
        sections=[
            {"type": "heading", "level": 1, "text": "Q2 Review"},
            {"type": "narrative", "markdown": "Revenue grew {{result:r1.total}}."},
            {"type": "metric_headline", "result_id": "m1", "label": "Revenue"},
            {"type": "chart", "result_id": "r1", "chart_type": "bar"},
            {"type": "table", "result_id": "r1"},
            {"type": "divider"},
        ],
    )
    secs = parse_sections(req.sections)
    assert [s.type for s in secs] == ["heading", "narrative", "metric_headline", "chart", "table", "divider"]


def test_reject_unknown_section_type():
    with pytest.raises(ValidationError):
        ComposeRequest(title="x", sections=[{"type": "bogus"}])


# --- Defensive section-type aliasing -------------------------------------
# The composing LLM consistently emits `type:"text"` / `type:"data"` (common-sense
# names) instead of the canonical `narrative` / `table`, and sometimes omits the
# required `narrative.markdown`. That fails the discriminated-union validation,
# the agent retries 2-4x, and the turn times out. We tolerate the aliases at the
# validation boundary AND store the canonical dicts back so downstream
# (assemble_spec / compose_report, which consume the raw dicts) sees real types.


def test_text_alias_maps_to_narrative():
    req = ComposeRequest(title="x", sections=[{"type": "text", "markdown": "Revenue grew {{result:r1.total}}."}])
    secs = parse_sections(req.sections)
    assert [s.type for s in secs] == ["narrative"]
    # validator normalizes in place so downstream raw-dict consumers see 'narrative'
    assert req.sections[0]["type"] == "narrative"


def test_text_alias_without_markdown_falls_back_to_body_field():
    # LLM emits the body under `text`/`content` instead of `markdown` — tolerate it.
    for body_field in ("text", "content", "body"):
        req = ComposeRequest(title="x", sections=[{"type": "text", body_field: "Quarterly summary."}])
        secs = parse_sections(req.sections)
        assert secs[0].type == "narrative"
        assert secs[0].markdown == "Quarterly summary."


def test_data_alias_maps_to_table():
    req = ComposeRequest(title="x", sections=[{"type": "data", "result_id": "r1"}])
    secs = parse_sections(req.sections)
    assert [s.type for s in secs] == ["table"]
    assert req.sections[0]["type"] == "table"


def test_canonical_types_unaffected_by_normalization():
    # An explicit narrative with markdown must pass through untouched.
    req = ComposeRequest(title="x", sections=[{"type": "narrative", "markdown": "Hi"}])
    assert req.sections[0] == {"type": "narrative", "markdown": "Hi"}


def test_bogus_type_still_rejected_after_alias_layer():
    with pytest.raises(ValidationError):
        ComposeRequest(title="x", sections=[{"type": "data_dump"}])


def test_unhashable_type_raises_clean_validation_error_not_typeerror():
    # A malformed section whose `type` is unhashable (list/dict) must NOT crash the
    # alias layer with TypeError (uncatchable 500 / non-retryable); it must flow to
    # pydantic as a clean ValidationError (422 the agent can retry on).
    with pytest.raises(ValidationError):
        ComposeRequest(title="x", sections=[{"type": ["table"], "result_id": "r1"}])


def test_text_alias_with_null_markdown_falls_back_to_body_field():
    # markdown present-but-not-a-string (e.g. null) must still trigger the body fallback.
    req = ComposeRequest(title="x", sections=[{"type": "text", "markdown": None, "content": "Summary."}])
    secs = parse_sections(req.sections)
    assert secs[0].type == "narrative"
    assert secs[0].markdown == "Summary."


# --- Task 4 (Risk 1): financial_statement joins the ComposeSection union ----------------
# Playbook-only in practice (compose_report rejects it before assembly — see
# test_report_service.py's chat-boundary test) but the SCHEMA itself must accept the
# shape assemble_spec consumes, since assemble_spec is shared infra between the chat and
# playbook compose paths.


def test_financial_statement_section_parses():
    req = ComposeRequest(
        title="x",
        sections=[
            {
                "type": "financial_statement",
                "result_id": "r1",
                "statement": "income_statement",
                "period": "Jun 2026",
                "compare": {"prior": "r2", "yoy": "r3", "trend": "r4"},
            }
        ],
    )
    secs = parse_sections(req.sections)
    assert secs[0].type == "financial_statement"
    assert secs[0].result_id == "r1"
    assert secs[0].statement == "income_statement"
    assert secs[0].period == "Jun 2026"
    assert secs[0].compare == {"prior": "r2", "yoy": "r3", "trend": "r4"}


def test_financial_statement_section_compare_defaults_to_empty_dict():
    req = ComposeRequest(
        title="x",
        sections=[
            {"type": "financial_statement", "result_id": "r1", "statement": "trial_balance", "period": "Jun 2026"}
        ],
    )
    secs = parse_sections(req.sections)
    assert secs[0].compare == {}


@pytest.mark.parametrize("field", ["result_id", "statement", "period"])
def test_financial_statement_section_requires_core_fields(field):
    section = {
        "type": "financial_statement",
        "result_id": "r1",
        "statement": "income_statement",
        "period": "Jun 2026",
    }
    del section[field]
    with pytest.raises(ValidationError):
        ComposeRequest(title="x", sections=[section])


def test_financial_statement_section_rejects_unknown_statement_type():
    with pytest.raises(ValidationError):
        ComposeRequest(
            title="x",
            sections=[
                {"type": "financial_statement", "result_id": "r1", "statement": "cash_flow", "period": "Jun 2026"}
            ],
        )
