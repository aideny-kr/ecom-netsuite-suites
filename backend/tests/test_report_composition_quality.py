"""Report-composition quality + reliability guardrails.

A lightweight, no-LLM "eval" that pins the deterministic contract behind reliable,
curated report generation:
  - the broken `netsuite.report` stub is NOT reachable (it is an unimplemented
    keyword-only stub the dispatcher cannot even call);
  - the reporting profile steers the LLM toward summary + chart and away from
    raw detail dumps, and enumerates the valid section types it kept guessing
    wrong (`text`/`data`).

These are intentionally content/registry assertions (not an LLM scoring loop) so
they run in CI without a model call. Composition behavior itself is exercised
live + by the T2 multi-angle gate.
"""

from pathlib import Path

import yaml

from app.mcp.governance import TOOL_CONFIGS
from app.mcp.registry import TOOL_REGISTRY
from app.services.chat import knowledge_profiles
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

_CANONICAL_SECTION_TYPES = ("heading", "narrative", "metric_headline", "chart", "table", "divider")


def _reporting_fragment() -> str:
    path = Path(knowledge_profiles.__file__).parent / "reporting.yaml"
    data = yaml.safe_load(path.read_text())
    return data["prompt_fragment"]


# --- The broken netsuite.report stub is fully de-registered ----------------
# netsuite_report.execute is `async def execute(*, ...)` (keyword-only); the
# governed dispatcher calls it positionally -> "execute() takes 0 positional
# arguments but 1 was given". It only ever returned "not yet implemented". When
# the LLM picked it, the step was wasted. The working native path is the external
# MCP ns_runReport (called directly) + the local netsuite.financial_report.


def test_netsuite_report_stub_not_in_registry():
    assert "netsuite.report" not in TOOL_REGISTRY


def test_netsuite_report_stub_not_chat_reachable():
    assert "netsuite.report" not in ALLOWED_CHAT_TOOLS


def test_netsuite_report_stub_has_no_governance_config():
    assert "netsuite.report" not in TOOL_CONFIGS


def test_financial_report_description_does_not_steer_to_broken_stub():
    # netsuite.financial_report is the WORKING SuiteQL path; its description must
    # not tell the model to "Prefer netsuite.report" (the broken stub).
    desc = TOOL_REGISTRY["netsuite.financial_report"]["description"].lower()
    assert "netsuite.report" not in desc
    assert "prefer netsuite.report" not in desc


# --- Reporting profile steers toward summary + charts (not raw dumps) ------
# A composed financial report dumped 600+ raw GL rows and zero charts. The
# profile must (a) enumerate the valid section types the LLM kept guessing wrong,
# (b) default to narrative + key figures + a chart of the major drivers, and
# (c) tell the model a report is NOT a raw data dump. Generic guidance only — it
# must never hardcode column names (prompt-pollution).


def test_reporting_profile_enumerates_valid_section_types():
    fragment = _reporting_fragment().lower()
    for section_type in _CANONICAL_SECTION_TYPES:
        assert section_type in fragment, f"reporting.yaml must name section type '{section_type}'"


def test_reporting_profile_defaults_to_summary_and_charts():
    fragment = _reporting_fragment().lower()
    assert "chart" in fragment
    assert "summar" in fragment  # summary / summarize


def test_reporting_profile_warns_against_raw_data_dumps():
    fragment = _reporting_fragment().lower()
    assert "dump" in fragment  # the explicit "not a raw data dump" guidance


def test_reporting_profile_has_no_hardcoded_financial_columns():
    # Guard the no-prompt-pollution rule: the profile must stay schema-agnostic.
    fragment = _reporting_fragment().lower()
    for leaked in ("periodname", "acctname", "acctnumber", "accttype", "transactionaccountingline"):
        assert leaked not in fragment


def test_report_compose_description_mentions_section_types_and_charts():
    desc = TOOL_REGISTRY["report.compose"]["description"].lower()
    assert "chart" in desc
    assert "result_id" in desc
    for section_type in ("narrative", "chart", "table"):
        assert section_type in desc


def test_reporting_profile_chart_types_match_schema_no_drift():
    # The profile restates the chart_type enum as prose (the schema can't reach the LLM
    # via the tool input_schema). Cross-check it against the pydantic source of truth so
    # the two can't drift (the gate caught the profile advertising 4 of 7 valid types).
    import re
    from typing import get_args

    from app.schemas.report import ChartSection

    annotation = ChartSection.model_fields["chart_type"].annotation  # Literal[...] | None
    schema_types: set[str] = set()
    for arg in get_args(annotation):
        schema_types |= set(get_args(arg))  # the Literal's members

    match = re.search(r'chart_type"?\s*:\s*"([a-z|]+)"', _reporting_fragment())
    assert match, "reporting.yaml must advertise a chart_type example"
    advertised = set(match.group(1).split("|"))
    assert advertised == schema_types, f"profile chart_types {advertised} != schema {schema_types}"
