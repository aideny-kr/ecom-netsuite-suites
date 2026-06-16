# backend/tests/test_report_tool_registration.py
from app.mcp.governance import TOOL_CONFIGS
from app.mcp.registry import TOOL_REGISTRY
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS
from app.services.chat.tool_categories import categorize
from app.services.chat.tools import _LOCAL_NAME_MAP, build_local_tool_definitions


def test_report_compose_registered_and_visible():
    assert "report.compose" in TOOL_REGISTRY
    assert "report.export" not in TOOL_REGISTRY
    assert "report.compose" in ALLOWED_CHAT_TOOLS
    names = {t["name"] for t in build_local_tool_definitions()}
    assert "report_compose" in names
    assert _LOCAL_NAME_MAP["report_compose"] == "report.compose"


def test_report_compose_categorized():
    assert categorize("report_compose") == "report"


def test_report_compose_has_governance_entry():
    """Gate E (finding #13): report.compose is an LLM-callable write+commit tool, so
    it MUST carry a governance config — not fall through to the relaxed 60/min default
    with no entitlement and unfiltered params. The predecessor report.export entry was
    deleted when the tool was renamed; this re-establishes the per-tool governance."""
    assert "report.export" not in TOOL_CONFIGS
    cfg = TOOL_CONFIGS["report.compose"]
    assert cfg["rate_limit_per_minute"] == 10
    assert cfg["requires_entitlement"] == "mcp_tools"
    assert cfg["timeout_seconds"] == 60
    assert cfg["default_limit"] is None
    assert cfg["max_limit"] is None
    # CRITICAL: validate_params strips anything not in allowlisted_params. The tool
    # consumes ONLY params["title"] + params["sections"], so the allowlist must be
    # exactly those two — a wrong list silently empties the compose payload.
    assert cfg["allowlisted_params"] == ["title", "sections"]


def test_report_compose_params_survive_filter():
    """The allowlist must let title + sections pass through validate_params unchanged
    (a regression here silently drops the report content)."""
    from app.mcp.governance import validate_params

    params = {"title": "Q2", "sections": [{"type": "heading", "level": 1, "text": "Q2"}]}
    filtered = validate_params("report.compose", params)
    assert filtered["title"] == "Q2"
    assert filtered["sections"] == params["sections"]
