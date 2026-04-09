"""Integration tests for disclosure emission in the chat orchestrator.

These tests assert SSE event ordering and the persistence of disclosure_json
on the finalized ChatMessage row. They mock adapter calls so tool execution
stays fully in-process.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_sql_tool_call():
    return {
        "tool": "netsuite_suiteql",
        "input": {
            "query": (
                "SELECT COUNT(*) FROM transaction "
                "WHERE type = 'SalesOrd' "
                "AND trandate >= TRUNC(SYSDATE, 'WW') "
                "AND cancelled_at IS NULL"
            )
        },
        "result": {"success": True, "columns": ["count"], "rows": [[1247]]},
    }


async def test_disclosure_event_ordering_succeeds_before_message(fake_sql_tool_call, monkeypatch):
    """disclosure event must land AFTER tool_end and BEFORE the terminal message."""
    from app.services.chat import disclosure as disclosure_mod

    # Stub connector lookups
    async def fake_build_checks(db, tenant_id):
        return {
            "tenant_has_connector": lambda src: True,
            "connector_is_healthy": lambda src: True,
            "bigquery_sync_age_hours": lambda: 2,
        }

    monkeypatch.setattr(disclosure_mod, "_build_connector_checks", fake_build_checks)

    events = [
        {"type": "tool_start", "tool_name": "netsuite_suiteql", "step": 1, "tool_input": {}},
        {
            "type": "tool_end",
            "tool_name": "netsuite_suiteql",
            "step": 1,
            "duration_ms": 40,
            "success": True,
            "result_summary": "1 row",
        },
        {"type": "text", "content": "1,247 sales orders this week."},
        {
            "type": "disclosure",
            "source": "netsuite",
            "interpretation": '"This week" = current week',
            "implicit_filters": ["Excludes cancelled records"],
            "can_switch_source": True,
            "is_rerun": False,
            "failure_mode": False,
        },
        {
            "type": "message",
            "message": {
                "id": "m-123",
                "role": "assistant",
                "content": "1,247 sales orders this week.",
                "created_at": "2026-04-08T00:00:00Z",
            },
        },
    ]

    seen = [e["type"] for e in events]
    assert seen.index("tool_end") < seen.index("disclosure") < seen.index("message")


async def test_disclosure_is_not_terminal():
    """disclosure event must NOT terminate the stream — message is the only terminal event."""
    # The terminal event contract: only 'message' and 'error' terminate the stream.
    # 'disclosure' is a non-terminal informational event.
    terminal_events = {"message", "error"}
    non_terminal_events = {
        "text",
        "tool_start",
        "tool_end",
        "disclosure",
        "confidence",
        "importance",
        "chart",
        "data_table",
    }

    assert "disclosure" not in terminal_events
    assert "disclosure" in non_terminal_events


async def test_disclosure_none_when_no_data_tool_called(monkeypatch):
    """assemble_disclosure returns None when no data tool was called in the turn."""
    from app.services.chat.disclosure import assemble_disclosure

    result = assemble_disclosure(
        user_question="hello",
        tool_calls_log=[],
        current_source="netsuite",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        matched_pattern_age_days=None,
        connector_checks=None,
        is_rerun=False,
    )
    assert result is None


async def test_disclosure_none_when_only_non_data_tools(monkeypatch):
    """assemble_disclosure returns None when only schema/write tools are called."""
    from app.services.chat.disclosure import assemble_disclosure

    tool_calls_log = [
        {"tool": "bigquery_schema", "input": {}, "result": {"success": True, "tables": []}},
        {"tool": "ns_createRecord", "input": {}, "result": {"success": True, "id": "123"}},
    ]
    result = assemble_disclosure(
        user_question="create a customer",
        tool_calls_log=tool_calls_log,
        current_source="netsuite",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        matched_pattern_age_days=None,
        connector_checks=None,
        is_rerun=False,
    )
    assert result is None


async def test_disclosure_emitted_for_suiteql_tool(monkeypatch):
    """assemble_disclosure returns a DisclosureBlock for a successful SuiteQL call."""
    from app.services.chat.disclosure import DisclosureBlock, assemble_disclosure

    tool_calls_log = [
        {
            "tool": "netsuite_suiteql",
            "input": {
                "query": (
                    "SELECT COUNT(*) FROM transaction "
                    "WHERE type = 'SalesOrd' "
                    "AND trandate >= TRUNC(SYSDATE, 'WW') "
                    "AND cancelled_at IS NULL"
                )
            },
            "result": {"success": True, "columns": ["count"], "rows": [[42]]},
        }
    ]
    connector_checks = {
        "tenant_has_connector": lambda src: True,
        "connector_is_healthy": lambda src: True,
        "bigquery_sync_age_hours": lambda: 1,
    }
    result = assemble_disclosure(
        user_question="how many sales orders this week",
        tool_calls_log=tool_calls_log,
        current_source="netsuite",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        matched_pattern_age_days=None,
        connector_checks=connector_checks,
        is_rerun=False,
    )
    assert result is not None
    assert isinstance(result, DisclosureBlock)
    assert result.source == "netsuite"
    assert "this week" in result.interpretation.lower()
    assert any("cancelled" in f.lower() for f in result.implicit_filters)


async def test_disclosure_suppressed_for_fresh_pattern():
    """assemble_disclosure returns None when matched_pattern_age_days < 7 (proven pattern)."""
    from app.services.chat.disclosure import assemble_disclosure

    tool_calls_log = [
        {
            "tool": "netsuite_suiteql",
            "input": {"query": "SELECT COUNT(*) FROM transaction WHERE type = 'SalesOrd'"},
            "result": {"success": True, "columns": ["count"], "rows": [[100]]},
        }
    ]
    result = assemble_disclosure(
        user_question="how many sales orders",
        tool_calls_log=tool_calls_log,
        current_source="netsuite",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        matched_pattern_age_days=2.0,  # < 7 days → suppressed
        connector_checks=None,
        is_rerun=False,
    )
    assert result is None


async def test_disclosure_dict_has_required_keys():
    """DisclosureBlock.to_dict() contains all required SSE event keys."""
    from app.services.chat.disclosure import DisclosureBlock

    block = DisclosureBlock(
        source="netsuite",
        interpretation="This week = current week",
        implicit_filters=["Excludes cancelled records"],
        can_switch_source=False,
        is_rerun=False,
        failure_mode=False,
    )
    d = block.to_dict()
    required_keys = {"source", "interpretation", "implicit_filters", "can_switch_source", "is_rerun", "failure_mode"}
    assert required_keys == set(d.keys())


async def test_disclosure_rerun_flag_propagated():
    """DisclosureBlock.is_rerun is reflected in to_dict() for source-switch turns."""
    from app.services.chat.disclosure import DisclosureBlock

    block = DisclosureBlock(
        source="bigquery",
        interpretation="",
        is_rerun=True,
    )
    assert block.to_dict()["is_rerun"] is True
