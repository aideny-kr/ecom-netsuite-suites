"""Verify clarify tool schema shape."""

from app.services.chat.plan_mode.clarify_tool import (
    CLARIFY_TOOL_SCHEMA,
    get_clarify_tool,
)


def test_clarify_tool_has_required_top_level_fields():
    s = CLARIFY_TOOL_SCHEMA
    assert s["name"] == "clarify"
    assert "description" in s
    assert "input_schema" in s
    assert s["input_schema"]["type"] == "object"


def test_clarify_required_inputs():
    s = CLARIFY_TOOL_SCHEMA
    props = s["input_schema"]["properties"]
    assert "options" in props
    assert "ambiguity_summary" in props
    assert s["input_schema"]["required"] == ["options", "ambiguity_summary"]


def test_options_array_constraints():
    options = CLARIFY_TOOL_SCHEMA["input_schema"]["properties"]["options"]
    assert options["type"] == "array"
    assert options["minItems"] == 2
    assert options["maxItems"] == 3


def test_option_item_shape():
    item = CLARIFY_TOOL_SCHEMA["input_schema"]["properties"]["options"]["items"]
    assert item["type"] == "object"
    props = item["properties"]
    assert "id" in props
    assert props["id"]["enum"] == ["A", "B", "C"]
    assert "title" in props
    assert "rationale" in props
    assert "source" in props
    assert "is_default" in props
    assert props["is_default"]["type"] == "boolean"
    assert set(item["required"]) == {"id", "title", "rationale", "source", "is_default"}


def test_source_enum():
    src = CLARIFY_TOOL_SCHEMA["input_schema"]["properties"]["options"]["items"]["properties"]["source"]
    assert src["enum"] == ["netsuite", "bigquery", "shopify", "stripe", "drive"]


def test_max_lengths_present():
    """Bounded strings keep payloads small enough for SSE + DB JSONB."""
    item_props = CLARIFY_TOOL_SCHEMA["input_schema"]["properties"]["options"]["items"]["properties"]
    assert item_props["title"]["maxLength"] == 80
    assert item_props["rationale"]["maxLength"] == 200
    summary = CLARIFY_TOOL_SCHEMA["input_schema"]["properties"]["ambiguity_summary"]
    assert summary["maxLength"] == 500


def test_get_clarify_tool_gated_by_flag():
    assert get_clarify_tool(plan_mode_enabled=False) is None


def test_get_clarify_tool_returns_schema_when_flag_on():
    tool = get_clarify_tool(plan_mode_enabled=True)
    assert tool is not None
    assert tool["name"] == "clarify"
    # Must be the same object as CLARIFY_TOOL_SCHEMA (single source of truth)
    assert tool is CLARIFY_TOOL_SCHEMA
