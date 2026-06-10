# backend/tests/test_report_tool_registration.py
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
