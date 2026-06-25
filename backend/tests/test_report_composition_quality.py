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

from app.mcp.governance import TOOL_CONFIGS
from app.mcp.registry import TOOL_REGISTRY
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS


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
