"""The dispatcher refuses an unbounded transaction-line scan filtered through BUILTIN.DF.

2026-10-05: a "Yucca orders day by day" chat turn took 7 minutes. Each query filtered
`BUILTIN.DF(i.custitem_fw_platform) = 'Yucca'` over transactionline with no date range and took
65-116 s; with a trandate floor the same filter returned the same rows in seconds. The query is
refused before it reaches NetSuite, with the fix in the error, on both SuiteQL tools.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.chat import tools

UNBOUNDED = (
    "SELECT TRUNC(t.trandate) AS d, COUNT(DISTINCT t.id) AS orders FROM transaction t "
    "JOIN transactionline tl ON tl.transaction = t.id JOIN item i ON i.id = tl.item "
    "WHERE t.type = 'SalesOrd' AND BUILTIN.DF(i.custitem_fw_platform) = 'Yucca' GROUP BY TRUNC(t.trandate)"
)
BOUNDED = UNBOUNDED.replace("AND BUILTIN", "AND t.trandate >= TO_DATE('2026-09-01', 'YYYY-MM-DD') AND BUILTIN")
CONTEXT = dict(db=SimpleNamespace(info={}), tenant_id="tenant", actor_id="actor", correlation_id="turn")


async def test_local_tool_refuses_the_unbounded_scan_without_calling_netsuite(monkeypatch):
    rpc = AsyncMock(return_value="{}")
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    result = json.loads(await tools.execute_tool_call("netsuite_suiteql", {"query": UNBOUNDED}, **CONTEXT))
    assert rpc.await_count == 0
    assert result["error"] and "trandate" in result["next_step"]
    assert result["perf_anti_patterns"] == ["unbounded_df_line_scan"]


async def test_external_mcp_tool_refuses_it_too(monkeypatch):
    rpc = AsyncMock(return_value="{}")
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    result = json.loads(
        await tools.execute_tool_call("ext__abc__ns_runCustomSuiteQL", {"sqlQuery": UNBOUNDED}, **CONTEXT)
    )
    assert rpc.await_count == 0 and result["error"]


async def test_a_date_bounded_query_runs(monkeypatch):
    rpc = AsyncMock(return_value=json.dumps({"rows": []}))
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    await tools.execute_tool_call("netsuite_suiteql", {"query": BOUNDED}, **CONTEXT)
    assert rpc.await_count == 1


async def test_replays_that_opt_out_run_unchanged(monkeypatch):
    rpc = AsyncMock(return_value=json.dumps({"rows": []}))
    monkeypatch.setattr(tools, "_execute_tool_call_once", rpc)
    await tools.execute_tool_call("netsuite_suiteql", {"query": UNBOUNDED}, perf_guard=False, **CONTEXT)
    assert rpc.await_count == 1
