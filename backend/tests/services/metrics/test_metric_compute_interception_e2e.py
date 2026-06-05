# backend/tests/services/metrics/test_metric_compute_interception_e2e.py
"""End-to-end seam test for the anti-hallucination invariant.

The two prior test halves were VACUOUS in isolation:

  * ``test_metric_interception.py`` hand-builds a payload via ``metric_data_table``
    and proves the orchestrator's ``_intercept_tool_result`` suppresses it. But it
    never runs the real ``compute_metric``.
  * ``test_metric_compute_integration.py`` runs the real ``compute_metric`` and
    asserts the returned dict shape — but never feeds that dict through the
    interceptor, so it never proves the computed number is withheld from the LLM.

The SEAM between them — ``compute_metric()``'s ACTUAL output, serialized exactly
as ``execute_tool_call`` serializes it (``json.dumps(result, default=str)``),
passed through the production interceptor (``_make_tool_interceptor`` →
``_intercept_tool_result``) — is the one the anti-hallucination invariant
actually depends on, and it was untested. If ``compute_metric`` ever stops
emitting ``suppress_llm_value`` (or the value lands somewhere the condensed
string echoes), BOTH prior halves still pass while the real number leaks to the
LLM. This test closes that seam: real compute → real serialization → real
interceptor → assert the literal computed number is in the SSE rows the FE
renders but ABSENT from every byte of the LLM-facing string.
"""

import json

from sqlalchemy import delete

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.chat.orchestrator import _make_tool_interceptor
from app.services.metrics.metric_compute import compute_metric


async def _ensure_system_tenant(db):
    from sqlalchemy import select

    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    # Idempotency: a seeded catalog collides on UNIQUE(tenant_id, key). Clear first
    # (rolled back per the db fixture) so this test is deterministic seeded-or-not.
    await db.execute(delete(MetricDefinition))
    await db.flush()


def _serialize_as_orchestrator(result: dict) -> str:
    """Byte-identical to execute_tool_call's serialization of a tool dict."""
    return json.dumps(result, default=str)


def _flatten(rows) -> list:
    return [cell for row in rows for cell in row]


async def test_real_query_metric_value_reaches_fe_but_not_llm(db, tenant_a, monkeypatch):
    """A REAL query-backed compute → real serialization → real interceptor:
    the computed number must render on the FE (SSE rows) but be absent from the
    LLM string. This is the seam the two prior tests left unproven."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT SUM(amount) FROM transactionline", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    # A distinctive number unlikely to collide with row_count/columns metadata.
    leaked_value = 1234567.89

    async def _ns_ok(params, context=None, **kwargs):
        return {"columns": ["s"], "rows": [[leaked_value]]}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_ok)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert "error" not in out, out

    # Serialize EXACTLY as the orchestrator does before interception.
    result_str = _serialize_as_orchestrator(out)

    interceptor = _make_tool_interceptor()
    intercept_data, llm_result_str = interceptor("metric_compute", result_str)

    # (a) it routed through the data_table interception branch (an SSE event fired).
    assert intercept_data is not None, "metric_compute result was NOT intercepted — number leaks raw to LLM"
    event_type, event_data = intercept_data
    assert event_type == "data_table"

    # (b) the SSE rows the FE renders DO carry the real computed number.
    assert leaked_value in _flatten(event_data["rows"])

    # (c) the LLM-facing string must NOT contain the number in ANY form.
    assert str(leaked_value) not in llm_result_str, (
        "computed metric value leaked into the LLM-facing string (anti-hallucination breach)"
    )
    assert "1234567" not in llm_result_str
    parsed_llm = json.loads(llm_result_str)
    assert "rows" not in parsed_llm
    assert "rows_preview" not in parsed_llm
    # the condensed string still tells the LLM the shape + do-not-recompute note.
    assert parsed_llm.get("row_count") == 1
    assert parsed_llm.get("columns") == ["Metric", "Value", "Unit", "Period"]
    assert "note" in parsed_llm


async def test_real_expression_metric_value_not_leaked_to_llm(db, tenant_a, monkeypatch):
    """Same seam for an EXPRESSION metric: compute net_margin from real leaves via
    the safe evaluator, serialize, intercept — the ratio must not reach the LLM."""
    await _ensure_system_tenant(db)
    for key in ("net_income", "gross_revenue"):
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=key,
                display_name=key,
                definition="x",
                unit="currency",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
                params_schema={"period": {"type": "period"}},
                status="active",
                version=1,
            )
        )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_margin",
            display_name="Net Margin",
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="net_income / gross_revenue",
            depends_on=["net_income", "gross_revenue"],
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return {"net_income": 31.0, "gross_revenue": 124.0}[metric.key]

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_margin",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert "error" not in out, out
    computed = out["rows"][0][1]  # the real evaluated ratio
    assert round(computed, 6) == round(31.0 / 124.0, 6)

    result_str = _serialize_as_orchestrator(out)
    interceptor = _make_tool_interceptor()
    intercept_data, llm_result_str = interceptor("metric_compute", result_str)

    assert intercept_data is not None
    event_type, event_data = intercept_data
    assert event_type == "data_table"
    assert computed in _flatten(event_data["rows"])

    # The full float repr (as default=str would render it) must not be in the LLM string.
    assert str(computed) not in llm_result_str
    # And no leading-digit fragment of the ratio either.
    assert repr(computed)[:6] not in llm_result_str
    assert "rows" not in json.loads(llm_result_str)
