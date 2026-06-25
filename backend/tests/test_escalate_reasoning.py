# backend/tests/test_escalate_reasoning.py
from app.services.chat import thinking
from app.services.chat.tool_categories import categorize
from app.services.chat.tools import build_local_tool_definitions


def test_escalate_reasoning_tool_is_advertised():
    names = {t["name"] for t in build_local_tool_definitions()}
    assert "escalate_reasoning" in names


def test_escalate_reasoning_schema_has_optional_rationale():
    tool = next(t for t in build_local_tool_definitions() if t["name"] == "escalate_reasoning")
    props = tool["input_schema"]["properties"]
    assert "rationale" in props
    assert tool["input_schema"].get("required", []) == []  # no required args


def test_escalate_reasoning_categorized():
    assert categorize("escalate_reasoning") == "control"


def test_next_level_used_for_bump():
    # The loop bumps via thinking.next_level — assert the contract it relies on.
    assert thinking.next_level("med") == "high"
    assert thinking.next_level("high") == "xhigh"
