# backend/tests/services/chat/test_metric_interception.py
"""The metric value must NOT leak into the LLM-facing condensed string.

A metric is a 1-row data_table whose whole point is the single computed number.
The anti-hallucination invariant ("never let the LLM present tool-computed
numbers") requires that number to reach the frontend via the SSE event_data
rows, while the condensed string the LLM sees carries commentary instructions
ONLY — no value, no rows, no rows_preview.

These tests assert the REAL invariant: the literal value string is present in
the SSE rows but absent from the condensed LLM string.
"""

import json

from sqlalchemy import select

from app.services.chat.orchestrator import ContextNeed, _intercept_tool_result
from app.services.metrics.metric_compute import metric_data_table


def test_metric_value_reaches_frontend_but_not_llm_default_context():
    payload = metric_data_table("Net Margin", 0.2531, "percent", "last_quarter", "expr")

    event_type, sse_event_data, condensed = _intercept_tool_result("metric_compute", json.dumps(payload))

    # (a) routes through the data_table interception branch
    assert event_type == "data_table"

    # (b) the SSE rows the frontend renders DO carry the real value
    assert sse_event_data is not None
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 0.2531 in flat

    # (c) the LLM-facing condensed string must NOT contain the value
    assert "0.2531" not in condensed
    # ...and must not smuggle it via rows / rows_preview either
    parsed_condensed = json.loads(condensed)
    assert "rows" not in parsed_condensed
    assert "rows_preview" not in parsed_condensed
    # it should still tell the LLM the shape + the do-not-recompute instruction
    assert parsed_condensed.get("row_count") == 1
    assert parsed_condensed.get("columns") == ["Metric", "Value", "Unit", "Period"]
    assert "note" in parsed_condensed


def test_metric_value_not_leaked_in_full_context():
    # FULL context normally hands the LLM every row; suppression must still apply.
    payload = metric_data_table("Net Margin", 0.2531, "percent", "last_quarter", "expr")

    event_type, sse_event_data, condensed = _intercept_tool_result(
        "metric_compute", json.dumps(payload), ContextNeed.FULL
    )

    assert event_type == "data_table"
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 0.2531 in flat
    assert "0.2531" not in condensed
    assert "rows" not in json.loads(condensed)


def test_query_backed_metric_shape_also_suppressed():
    # A query-backed metric is labeled by its key (a STRING, not the blessed_spec dict —
    # F4 (c)) and still suppresses its single computed value from the LLM string.
    payload = metric_data_table(
        "Gross Revenue",
        1234567.89,
        "currency",
        "last_quarter",
        "gross_revenue",
    )

    event_type, sse_event_data, condensed = _intercept_tool_result("metric_compute", json.dumps(payload))

    assert event_type == "data_table"
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 1234567.89 in flat
    assert "1234567.89" not in condensed
    assert "rows" not in json.loads(condensed)


def test_suppress_flag_set_on_metric_data_table():
    payload = metric_data_table("Net Margin", 0.2531, "percent", "last_quarter", "expr")
    assert payload.get("suppress_llm_value") is True


async def test_compute_metric_query_field_is_string_not_blessed_spec(db, tenant_a, monkeypatch):
    """REAL trust-boundary invariant (F4 (c)), driven through the PRODUCTION call site.
    compute_metric builds the data_table via metric_data_table(...); the prior call site
    passed `metric.blessed_spec` (the internal execution spec dict, e.g.
    {'query': 'SELECT SUM(amount) FROM transactionline ...', 'dialect': 'suiteql'}) as the
    `query` field. The orchestrator copies parsed['query'] verbatim into the SSE
    event_data['query'] that reaches the frontend — so the raw blessed SuiteQL text
    (table names, dialect) shipped to the client.

    The `query` field must instead be a STRING label (the metric key); the blessed_spec /
    its raw SQL must NEVER appear in the payload. We compute a real query-backed metric
    end-to-end (stubbed executor) and assert the resulting payload's `query` is a string,
    equals the metric key, and contains none of the blessed SQL. Pre-fix this FAILS:
    compute_metric handed the blessed_spec dict, so `query` was a dict carrying the SQL."""
    import json as _json

    from sqlalchemy import delete

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.models.tenant import Tenant
    from app.services.metrics.metric_compute import compute_metric

    # Ensure the SYSTEM tenant parent exists (rolled back per test) for the FK.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    # The shared local DB may already be seeded; clear metric rows (rolled back per test)
    # so our UNIQUE(tenant_id, key) insert below does not collide with a seeded row.
    await db.execute(delete(MetricDefinition))
    await db.flush()

    blessed_sql = "SELECT SUM(amount) FROM transactionline WHERE trandate>=:period_start"
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": blessed_sql, "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _ns_ok(params, context=None, **kwargs):
        return {"columns": ["s"], "rows": [[1234567.89]]}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_ok)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    assert "error" not in out, out
    # (a) the query field is a STRING label, never the blessed_spec dict.
    assert isinstance(out["query"], str), out["query"]
    assert out["query"] == "gross_revenue"
    # (b) the blessed SQL / dialect / table names are NOWHERE in the payload.
    serialized = _json.dumps(out, default=str)
    assert blessed_sql not in serialized
    assert "transactionline" not in serialized
    assert "dialect" not in serialized

    # (c) it still round-trips through the interceptor as a suppressed data_table; the
    #     SSE query field stays a plain string, the number reaches the FE not the LLM.
    event_type, sse_event_data, condensed = _intercept_tool_result("metric_compute", _json.dumps(out))
    assert event_type == "data_table"
    assert isinstance(sse_event_data["query"], str)
    assert sse_event_data["query"] == "gross_revenue"
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 1234567.89 in flat
    assert "1234567.89" not in condensed


def test_non_metric_data_table_is_unaffected():
    # A normal SuiteQL data_table (no suppress flag) must keep leaking its rows
    # into rows_preview exactly as before — suppression is opt-in only.
    payload = {
        "columns": ["order_id", "amount"],
        "rows": [["SO-1", 100.0], ["SO-2", 250.5]],
        "row_count": 2,
        "query": "SELECT order_id, amount FROM ...",
        "truncated": False,
    }

    event_type, sse_event_data, condensed = _intercept_tool_result("netsuite_suiteql", json.dumps(payload))

    assert event_type == "data_table"
    parsed_condensed = json.loads(condensed)
    # behavior byte-identical to before: rows_preview present, values visible
    assert "rows_preview" in parsed_condensed
    assert parsed_condensed["rows_preview"] == [["SO-1", 100.0], ["SO-2", 250.5]]
