"""Source-bound pivots: data fidelity, controls, storage and actor isolation."""

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.tools import pivot_tool, result_pivot
from app.services.chat.metabase_results import bind_result
from app.services.chat.tool_call_results import build_tool_call_log_entry, extract_result_payload

CID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TOOL = f"ext__{CID.hex}__query"
CONN = SimpleNamespace(
    id=CID,
    server_url="https://example.metabaseapp.com/api/metabase-mcp",
    provider="custom",
    auth_type="oauth2",
    metadata_json={"oauth_provider": "metabase"},
    status="active",
    is_enabled=True,
)


def make_result(rows, operation="distinct", grouped=True, **changes):
    query = {
        "lib/type": "mbql/query",
        "database": 2,
        "stages": [
            {
                "lib/type": "mbql.stage/mbql",
                "source-table": 45,
                "filters": [["=", {"lib/uuid": "filter"}, ["field", {}, 77], 395]],
                "aggregation": [[operation, {"lib/uuid": "measure"}, ["field", {}, 2]]],
            }
        ],
    }
    cols = [{"name": "orders", "source": "aggregation"}]
    if grouped:
        query["stages"][0]["breakout"] = [["field", {}, 10], ["field", {}, 11]]
        cols = [{"name": "sku", "source": "breakout"}, {"name": "state", "source": "breakout"}, *cols]
    raw = {"status": "completed", "data": {"cols": cols, "rows": rows}, **changes}
    bound = bind_result(raw, {"query": query}, CONN)
    return bound, query


def payload(rows, operation="distinct", grouped=True):
    raw, query = make_result(rows, operation, grouped)
    return extract_result_payload(TOOL, {"query": query}, json.dumps(raw))


def config(**overrides):
    return result_pivot.ResultPivot(
        **{
            "result_id": "r1",
            "control_result_id": "r2",
            "row_field": "sku",
            "column_field": "state",
            "value_field": "orders",
            **overrides,
        }
    )


def test_distinct_sku_overlap_preserves_cells_and_separate_control():
    source = payload([["SKU-A", "complete", 3], ["SKU-A", "canceled", 1], ["SKU-B", "complete", 3]])
    result = result_pivot.reshape(config(), source, payload([[4]], grouped=False))
    assert result["columns"] == ["sku", "canceled", "complete"]
    assert result["rows"] == [["SKU-A", 1, 3], ["SKU-B", None, 3]]
    assert "separately: 4" in result["caveats"][1]
    assert result["query"] == "" and result["source_kind"] == "metabase"
    assert result["pivot_provenance"]["query"] == source["metabase_source"]["query"]
    assert "overlap and exceed" in result["caveats"][-1]


def test_disjoint_distinct_groups_report_actual_control_agreement():
    result = result_pivot.reshape(
        config(), payload([["A", "canceled", 4], ["A", "complete", 4]]), payload([[8]], grouped=False)
    )
    assert "reconcile" in result["caveats"][-1]
    assert "overlap" not in result["caveats"][-1]


@pytest.mark.parametrize(
    "operation, rows, total",
    [
        ("sum", [["A", "x", "0.1"], ["A", "y", "0.2"]], "0.3"),
        ("count", [["A", "x", 2], ["A", "y", 3]], 5),
        ("avg", [["A", "x", 10], ["A", "y", 20]], 19),
        ("min", [["A", "x", -4], ["A", "y", 20]], -4),
        ("max", [["A", "x", -4], ["A", "y", 20]], 20),
    ],
)
def test_server_aggregate_semantics(operation, rows, total):
    result = result_pivot.reshape(config(), payload(rows, operation), payload([[total]], operation, grouped=False))
    assert result["rows"][0][1:] == [rows[0][2], rows[1][2]]
    assert str(total) in result["caveats"][1]


def test_additive_row_totals_use_decimal():
    result = result_pivot.reshape(
        config(include_total=True),
        payload([["A", "x", "0.1"], ["A", "y", "0.2"]], "sum"),
        payload([["0.3"]], "sum", grouped=False),
    )
    assert result["rows"] == [["A", "0.1", "0.2", "0.3"]]


@pytest.mark.parametrize("operation,total", [("distinct", 2), ("avg", 2), ("min", 2), ("max", 2)])
def test_nonadditive_row_totals_are_refused(operation, total):
    with pytest.raises(ValueError, match="separate server aggregates"):
        result_pivot.reshape(
            config(include_total=True),
            payload([["A", "x", 2]], operation),
            payload([[total]], operation, grouped=False),
        )


@pytest.mark.parametrize("change", ["connector", "endpoint", "database", "filter", "measure", "incomplete"])
def test_invalid_control_is_refused(change):
    source, control = payload([["A", "x", 2]]), payload([[2]], grouped=False)
    meta = control["metabase_source"]
    if change == "connector":
        meta["connector_id"] = str(uuid.uuid4())
    elif change == "endpoint":
        meta["server_url"] = "https://other.metabaseapp.com/api/metabase-mcp"
    elif change == "database":
        meta["query"]["database"] = 3
    elif change == "filter":
        meta["query"]["stages"][0]["filters"][0][-1] = 396
    elif change == "measure":
        meta["query"]["stages"][0]["aggregation"][0][-1][-1] = 3
    else:
        meta["complete"] = False
    with pytest.raises(ValueError):
        result_pivot._validate_payload(control)
        result_pivot.reshape(config(), source, control)


@pytest.mark.parametrize("flag", ["continuation_token", "has_more", "rows_truncated", "truncated"])
def test_partial_native_results_remain_partial(flag):
    raw, query = make_result([["A", "x", 1]], **{flag: True})
    frozen = extract_result_payload(TOOL, {"query": query}, json.dumps(raw))
    with pytest.raises(ValueError, match="partial"):
        result_pivot._validate_payload(frozen)


def test_storage_cap_is_not_a_complete_result():
    raw, query = make_result([[f"SKU-{n}", "complete", 1] for n in range(2001)])
    frozen = extract_result_payload(TOOL, {"query": query}, json.dumps(raw))
    assert len(frozen["rows"]) == 2000 and frozen["row_count"] == 2001
    with pytest.raises(ValueError, match="partial"):
        result_pivot._validate_payload(frozen)


@pytest.mark.parametrize("rows,total", [([], 0), ([["A", None, 1]], 1)])
def test_empty_and_null_dimensions(rows, total):
    result = result_pivot.reshape(config(), payload(rows), payload([[total]], grouped=False))
    assert result["rows"] == ([] if not rows else [["A", 1]])
    assert result["columns"] == (["sku"] if not rows else ["sku", "(null)"])


def test_duplicate_cells_and_label_collisions_are_not_silently_combined():
    for rows in ([["A", "x", 1], ["A", "x", 1]], [["A", None, 1], ["A", "(null)", 1]]):
        with pytest.raises(ValueError, match="Duplicate|collide"):
            result_pivot.reshape(config(), payload(rows), payload([[2]], grouped=False))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "bad", True])
def test_bad_measures_do_not_turn_into_zeros(value):
    with pytest.raises(ValueError, match="finite"):
        result_pivot.reshape(config(), payload([["A", "x", value]]), payload([[1]], grouped=False))


def test_metabase_interception_log_and_evidence_share_result_id():
    from app.services.chat.metabase_evidence import MetabaseEvidence
    from app.services.chat.orchestrator import _make_tool_interceptor

    raw, query = make_result([["A", "complete", 2]])
    original = json.dumps(raw)
    stored = []
    intercept = _make_tool_interceptor(cache_callback=lambda *args: stored.append(args), start_count=3)
    event, condensed = intercept(TOOL, original, {"query": query}, original)
    assert event is None  # Intermediate aggregates stay behind the evidence/control boundary.
    assert stored[0][3] == "r4"
    assert stored[0][-1]["metabase_source"]["connector_id"] == str(CID)
    log = build_tool_call_log_entry(step=0, tool_name=TOOL, params={"query": query}, result_str=original, duration_ms=1)
    assert log["result_payload"]["rows"] == raw["data"]["rows"]
    grounded = json.loads(MetabaseEvidence({TOOL}).observe(TOOL, {"query": query}, original, result_id="r4"))
    assert grounded["result_id"] == "r4" and "mb_ref" in grounded["table_reference"]
    assert json.loads(condensed)["result_id"] == "r4"


@pytest.mark.asyncio
async def test_result_path_never_calls_sql_executors_or_accepts_sql():
    with (
        patch.object(pivot_tool, "_execute_suiteql_pivot", new_callable=AsyncMock) as ns,
        patch.object(pivot_tool, "_execute_bigquery_pivot", new_callable=AsyncMock) as bq,
    ):
        result = await pivot_tool.execute({**config().model_dump(), "query": "SELECT 1"}, {})
    assert "error" in result
    ns.assert_not_called()
    bq.assert_not_called()


@pytest.mark.asyncio
async def test_scope_denial_happens_before_cache_read():
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    ctx = {"db": db, **{k: str(uuid.uuid4()) for k in ("tenant_id", "actor_id", "conversation_id")}}
    with patch.object(result_pivot, "get_full_payload_entry") as cache:
        result = await result_pivot.execute(config().model_dump(), ctx)
    assert "cannot access" in result["error"]
    cache.assert_not_called()


@pytest.mark.asyncio
async def test_saved_cross_turn_source_is_resolved_without_preview_or_requery():
    source = payload([["A", "x", 2]])
    message = SimpleNamespace(tool_calls=[{"result_id": "r1", "tool": TOOL, "result_payload": source}])
    db = SimpleNamespace(scalar=AsyncMock(return_value=CONN))
    with (
        patch.object(result_pivot, "get_full_payload_entry", return_value=None),
        patch.object(result_pivot, "load_conversation_tool_messages", new_callable=AsyncMock, return_value=[message]),
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
    ):
        found = await result_pivot._entry(db, uuid.uuid4(), uuid.uuid4(), "r1")
    assert found == source


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "revoked", "endpoint", "missing"])
async def test_connection_revocation_and_rebinding_reject_cached_result(change):
    connector = copy.copy(CONN)
    if change == "disabled":
        connector.is_enabled = False
    elif change == "revoked":
        connector.status = "error"
    elif change == "endpoint":
        connector.server_url = "https://other.metabaseapp.com/api/metabase-mcp"
    else:
        connector = None
    entry = {"tool": TOOL, "payload": payload([["A", "x", 2]])}
    db = SimpleNamespace(scalar=AsyncMock(return_value=connector))
    with (
        patch.object(result_pivot, "get_full_payload_entry", return_value=entry),
        pytest.raises(ValueError, match="unavailable"),
    ):
        await result_pivot._entry(db, uuid.uuid4(), uuid.uuid4(), "r1")


def test_result_pivot_does_not_pin_netsuite_and_history_keeps_original_ids():
    from app.services.chat.history_tool_trace import render_tool_trace
    from app.services.chat.orchestrator import _compute_source_pin_update

    call = {"tool": "pivot_query_result", "params": {"result_id": "r1"}}
    assert _compute_source_pin_update([call]) == "leave_pin"
    history = render_tool_trace(
        [{"tool": TOOL, "result_id": "r7", "params": {}, "result_payload": payload([["A", "x", 1]])}]
    )
    assert '"result_id": "r7"' in history
