import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.chat import tools


async def test_same_rejected_query_is_not_retried_but_new_schema_evidence_allows_retry(monkeypatch):
    rpc = AsyncMock(return_value=json.dumps({"error": "Invalid search query: Failed to parse SQL near FETCH"}))
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    context = dict(db=SimpleNamespace(info={}), tenant_id="tenant", actor_id="actor", correlation_id="turn")
    params = dict(query="SELECT id, createdfrom FROM transaction", connection_id="c", expected_account_id="1")
    first = json.loads(await tools.execute_tool_call("netsuite_suiteql", params, **context))
    assert "query_recovery" in first
    repeated = json.loads(await tools.execute_tool_call("netsuite_suiteql", {**params, "limit": 5}, **context))
    assert "no external retry" in repeated["error"]
    assert rpc.await_count == 1
    rpc.return_value = "{}"
    await tools.execute_tool_call("netsuite_get_metadata", {}, **context)
    await tools.execute_tool_call("netsuite_suiteql", params, **context)
    assert rpc.await_count == 3


async def test_failures_are_isolated_by_turn_connection_and_tenant(monkeypatch):
    rpc = AsyncMock(return_value=json.dumps({"error": "Invalid search query"}))
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    context = dict(db=SimpleNamespace(info={}), tenant_id="tenant", actor_id="actor", correlation_id="turn")
    params = dict(query="SELECT bad FROM transaction", connection_id="c", expected_account_id="1")
    await tools.execute_tool_call("netsuite_suiteql", params, **context)
    await tools.execute_tool_call("netsuite_suiteql", {**params, "connection_id": "d"}, **context)
    await tools.execute_tool_call("netsuite_suiteql", params, **{**context, "correlation_id": "next"})
    await tools.execute_tool_call("netsuite_suiteql", params, **{**context, "tenant_id": "other"})
    assert rpc.await_count == 4


async def test_transient_errors_and_successful_reads_are_never_memoized(monkeypatch):
    rpc = AsyncMock(return_value=json.dumps({"error": "HTTP 503 temporarily unavailable"}))
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    context = dict(db=SimpleNamespace(info={}), tenant_id="tenant", actor_id="actor", correlation_id="turn")
    for _ in range(2):
        await tools.execute_tool_call("netsuite_suiteql", {"query": "SELECT id FROM transaction"}, **context)
    rpc.return_value = "{}"
    for _ in range(2):
        await tools.execute_tool_call("netsuite_suiteql", {"query": "SELECT id FROM transaction"}, **context)
    assert rpc.await_count == 4
