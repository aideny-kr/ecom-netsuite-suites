"""Tests for the cross_source_query tool (mocked source fetches)."""

import pytest

from app.mcp.tools import cross_source_tool


@pytest.mark.asyncio
async def test_execute_joins_two_sources(monkeypatch):
    async def fake_run_source(query, dialect, context):
        if dialect == "suiteql":
            return {
                "columns": ["sku", "ns_sales"],
                "rows": [["A", "100"], ["B", "200"]],
                "truncated": False,
            }
        return {"columns": ["item", "bq_spend"], "rows": [["A", "10"]], "truncated": False}

    monkeypatch.setattr(cross_source_tool, "_run_source", fake_run_source)

    out = await cross_source_tool.execute(
        {
            "left_query": "SELECT ...",
            "left_dialect": "suiteql",
            "right_query": "SELECT ...",
            "right_dialect": "bigquery",
            "join_keys": [{"left": "sku", "right": "item"}],
            "join_type": "inner",
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert out["joined"] is True
    assert out["row_count"] == 1
    assert out["rows"] == [["A", "100", "10"]]
    assert out["left_row_count"] == 2 and out["right_row_count"] == 1
    assert out["warnings"] == []


@pytest.mark.asyncio
async def test_execute_requires_context():
    out = await cross_source_tool.execute({"left_query": "x"}, context={})
    assert "error" in out


@pytest.mark.asyncio
async def test_execute_requires_join_keys():
    out = await cross_source_tool.execute(
        {
            "left_query": "a",
            "right_query": "b",
            "left_dialect": "suiteql",
            "right_dialect": "bigquery",
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert "error" in out and "join_keys" in out["error"]


@pytest.mark.asyncio
async def test_execute_surfaces_source_error(monkeypatch):
    async def boom(query, dialect, context):
        raise ValueError("No active BigQuery connector")

    monkeypatch.setattr(cross_source_tool, "_run_source", boom)
    out = await cross_source_tool.execute(
        {
            "left_query": "a",
            "left_dialect": "bigquery",
            "right_query": "b",
            "right_dialect": "suiteql",
            "join_keys": [{"left": "x", "right": "y"}],
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert "error" in out and "Left source" in out["error"]


@pytest.mark.asyncio
async def test_execute_warns_on_truncation_and_no_match(monkeypatch):
    async def fake_run_source(query, dialect, context):
        if dialect == "suiteql":
            return {"columns": ["sku", "v"], "rows": [["A", "1"]], "truncated": True}
        return {"columns": ["item", "w"], "rows": [["Z", "2"]], "truncated": False}

    monkeypatch.setattr(cross_source_tool, "_run_source", fake_run_source)
    out = await cross_source_tool.execute(
        {
            "left_query": "a",
            "left_dialect": "suiteql",
            "right_query": "b",
            "right_dialect": "bigquery",
            "join_keys": [{"left": "sku", "right": "item"}],
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert out["left_truncated"] is True
    assert any("truncated" in w for w in out["warnings"])
    assert any("No rows matched" in w for w in out["warnings"])


def test_tool_registered():
    from app.mcp.registry import TOOL_REGISTRY

    assert "cross_source.query" in TOOL_REGISTRY
    schema = TOOL_REGISTRY["cross_source.query"]["params_schema"]
    for p in ("left_query", "left_dialect", "right_query", "right_dialect", "join_keys"):
        assert p in schema


def test_tool_in_allowlist():
    from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

    assert "cross_source.query" in ALLOWED_CHAT_TOOLS


def test_tool_categorized_data_table():
    from app.services.chat.tool_categories import categorize

    assert categorize("cross_source_query") == "data_table"
    assert categorize("cross_source.query") == "data_table"


@pytest.mark.asyncio
async def test_execute_surfaces_join_error(monkeypatch):
    """A downstream join/pivot error returns a structured {'error': ...}, never crashes."""

    async def fake_run_source(query, dialect, context):
        if dialect == "suiteql":
            return {"columns": ["sku", "v"], "rows": [["A", "1"]], "truncated": False}
        return {"columns": ["item", "w"], "rows": [["A", "2"]], "truncated": False}

    monkeypatch.setattr(cross_source_tool, "_run_source", fake_run_source)
    out = await cross_source_tool.execute(
        {
            "left_query": "a",
            "left_dialect": "suiteql",
            "right_query": "b",
            "right_dialect": "bigquery",
            "join_keys": [{"left": "sku", "right": "item"}],
            "pivot": {"row_field": "nope", "column_field": "v", "value_field": "w"},
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert "error" in out
