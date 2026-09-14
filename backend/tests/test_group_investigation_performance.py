"""Regressions for the 55-order empty-card dead end and repeated reference reads."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import ToolUseBlock
from app.services.chat.transaction_context import transaction_tools
from app.services.transaction_ops import accounting_group as group
from app.services.transaction_ops.group_investigation import summarize
from app.services.transaction_ops.read_batch import reference_read, reference_read_batch
from tests.test_mutation_intercept import _llm_response, _stream_replay


async def test_reference_reads_coalesce_concurrent_cases_and_return_independent_copies():
    fetch = AsyncMock(return_value={"id": "1", "closed": False})
    scope = ("tenant", "connection", "account", "credential")
    with reference_read_batch() as batch:
        results = await asyncio.gather(
            *[reference_read(scope, "GET", "/record/v1/accountingPeriod/1", None, None, fetch) for _ in range(55)]
        )
        assert fetch.await_count == 1 and batch.hits == 54
        results[0]["closed"] = True
        assert results[1]["closed"] is False
    # Final approval preflight is outside preparation and must hit the server.
    await reference_read(scope, "GET", "/record/v1/accountingPeriod/1", None, None, fetch)
    assert fetch.await_count == 2


@pytest.mark.parametrize("field", range(4))
async def test_reference_cache_partitions_tenant_connection_account_and_credentials(field):
    fetch = AsyncMock(return_value={"id": "1"})
    first = ["tenant", "connection", "account", "credential"]
    second = first.copy()
    second[field] = "different"
    with reference_read_batch():
        for scope in (first, second):
            await reference_read(tuple(scope), "GET", "/record/v1/currency/1", None, None, fetch)
    assert fetch.await_count == 2


@pytest.mark.parametrize("method,path", [("GET", "/record/v1/invoice/1"), ("POST", "/query/v1/suiteql")])
async def test_transactions_and_queries_are_never_cached(method, path):
    fetch = AsyncMock(return_value={"id": "1"})
    with reference_read_batch():
        for _ in range(2):
            await reference_read(("scope",), method, path, None, None, fetch)
    assert fetch.await_count == 2


async def test_failure_is_not_cached_and_cancellation_releases_reference_lock():
    fetch = AsyncMock(side_effect=[ValueError("unavailable"), {"id": "1"}])
    with reference_read_batch():
        with pytest.raises(ValueError):
            await reference_read(("scope",), "GET", "/record/v1/currency/1", None, None, fetch)
        assert await reference_read(("scope",), "GET", "/record/v1/currency/1", None, None, fetch) == {"id": "1"}


def test_summary_preserves_pennies_and_does_not_treat_scale_as_difference():
    evidence = {
        "source_refresh": {"total": "10.01", "tax_total": "0.51", "included_tax_total": "0.510", "line_items": []},
        "sections": {
            "posting_documents": [
                {"record_type": "invoice", "total": "10.00", "taxTotal": "0.50", "amountPaid": "0.000"}
            ]
        },
    }
    result = summarize(evidence)
    assert result["variance"] == {"total": "0.01", "tax": "0.01"}
    assert not any("Net amounts" in r or "Payment is recorded" in r or "does not cover" in r for r in result["reasons"])


def test_transaction_inventory_keeps_accounting_research_and_sources_removes_unrelated_tools():
    ns = f"ext__{uuid4().hex}__ns_updateRecord"
    mb = f"ext__{uuid4().hex}__query"
    names = [
        "transaction_ops_accounting_group",
        "agent_skill",
        "web_search",
        "netsuite_suiteql",
        "celigo_flows",
        ns,
        mb,
    ]
    tools = [{"name": n, "description": "[metabase_mcp] query" if n == mb else ""} for n in names]
    unrelated = [{"name": n} for n in ["pricing_convert", "workspace_propose_patch", "sheets_create", "bigquery_sql"]]
    assert [t["name"] for t in transaction_tools(tools + unrelated)] == names


async def test_no_eligible_group_produces_audited_handoff_not_empty_card_and_reuses_same_turn(monkeypatch):
    tenant, actor, session = uuid4(), uuid4(), uuid4()
    db = AsyncMock(spec=AsyncSession)
    selection = {
        "group_id": "a" * 32,
        "scope": {"review_run_ids": [str(uuid4())]},
        "members": [{"case_id": str(uuid4()), "order_reference": f"R{i}"} for i in range(55)],
    }
    db.info = {"accounting_group_selection": selection}

    @asynccontextmanager
    async def factory():
        yield AsyncMock(spec=AsyncSession)

    evidence = AsyncMock(return_value={"success": True, "accounting_evidence": {"blockers": ["unverified_basis"]}})
    audit = AsyncMock()
    monkeypatch.setattr(group, "async_session_factory", factory)
    monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(group, "log_event", audit)
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools.execute_accounting_evidence", evidence)
    monkeypatch.setattr(
        "app.services.transaction_ops.tax_correction.candidate_confirmation", AsyncMock(return_value=None)
    )
    args = dict(
        db=db, tenant_id=tenant, actor_id=actor, session_id=str(session), correlation_id="test", tools=[], policy=None
    )
    assert await group.prepare_group_confirmation(**args) is None
    handoff = db.info["accounting_group_investigation"]
    assert handoff["eligible"] == 0 and handoff["case_count"] == 55 and handoff["financial_writes"] == 0
    assert sum(len(b["orders"]) for b in handoff["batches"]) == 55
    assert audit.await_args.kwargs["action"] == "accounting_group.investigation_required"
    db.info["accounting_group_selection"] = selection
    assert await group.prepare_group_confirmation(**args) is None
    assert evidence.await_count == 55
    db.add.assert_not_called()


@pytest.mark.parametrize("block_saved_read", [False, True])
async def test_actual_agent_continues_after_zero_candidates_without_confirmation(block_saved_read):
    db = AsyncMock(spec=AsyncSession)
    case_id, observation_id = str(uuid4()), str(uuid4())
    db.info = {
        "accounting_group_investigation": {
            "case_count": 55,
            "status": "investigation_required",
            "batches": [{"orders": [{"case_id": case_id, "audit_id": observation_id}]}],
        }
    }
    agent = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id=str(uuid4()))
    original_tool = {"name": "transaction_ops_accounting_evidence", "input_schema": {"type": "object"}}
    agent._tool_defs = [original_tool]
    adapter = MagicMock()
    replay = _stream_replay(
        [
            _llm_response(
                tool_blocks=[
                    ToolUseBlock(id="group", name="transaction_ops_accounting_group", input={"group_id": "a" * 32})
                ]
            ),
            _llm_response(
                tool_blocks=[
                    ToolUseBlock(
                        id="followup",
                        name="transaction_ops_accounting_evidence",
                        input={"case_id": case_id, "observation_id": observation_id, "section": "source"},
                    )
                ]
            ),
            _llm_response(text="The paid invoices need application evidence before a correction can be proposed."),
        ]
    )
    requests = []

    async def stream(**kwargs):
        requests.append(kwargs)
        async for event in replay(**kwargs):
            yield event

    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.side_effect = lambda results: {"role": "user", "content": results}
    execute = AsyncMock(return_value=json.dumps({"success": True, "financial_writes": 0}))

    def evaluate(_policy, _tool, params):
        return {"allowed": not (block_saved_read and params.get("observation_id")), "reason": "blocked"}

    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch("app.services.policy_service.evaluate_tool_call", evaluate),
        patch("app.services.chat.tools.execute_tool_call", execute),
        patch("app.services.transaction_ops.accounting_group.prepare_group_confirmation", AsyncMock(return_value=None)),
        patch("app.services.transaction_ops.tax_correction.candidate_confirmation", AsyncMock(return_value=None)),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent, task="Fix all orders in this group", context={}, db=db, adapter=adapter, model="test-model"
            )
        ]
    assert execute.await_count == (1 if block_saved_read else 4)
    assert not any(kind == "confirmation_required" for kind, _ in events)
    assert any("application evidence" in str(value) for kind, value in events if kind == "text")
    sent = adapter.build_tool_result_message.call_args_list[0].args[0]
    assert json.loads(sent[-1]["content"])["status"] == "investigation_required"
    details = json.loads(sent[-1]["content"])["representative_observations"]
    assert [d["section"] for d in details] == ["source", "documents"]
    if block_saved_read:
        assert all("Policy blocked" in d["result_preview"] for d in details)
    assert all(d["case_id"] == case_id and d["observation_id"] == observation_id for d in details)
    assert all(request["tool_choice"] is None for request in requests)
    assert all(request["tools"] == [original_tool] for request in requests)
    assert len({request["thinking_level"] for request in requests}) == 1
    assert original_tool["input_schema"] == {"type": "object"}


@pytest.mark.parametrize(
    "definitions,batches",
    [
        ([], [{"orders": [{"case_id": "c", "audit_id": "a"}]}]),
        ([{"name": "transaction_ops_accounting_evidence"}], []),
        ([{"name": "transaction_ops_accounting_evidence"}], [{"orders": [{"case_id": "c"}]}]),
    ],
)
def test_followup_does_not_invent_tools_or_observations(definitions, batches):
    from app.services.transaction_ops.group_investigation import representative_reads

    assert representative_reads({"batches": batches}, definitions) == []


async def test_saved_observation_is_tenant_case_scoped_and_audited(db, tenant_a, tenant_b):
    from app.services.audit_service import log_event
    from app.services.transaction_ops.group_investigation import read_observation

    case_id = uuid4()
    event = await log_event(
        db,
        tenant_a.id,
        category="transaction_ops",
        action="accounting.evidence.observed",
        resource_type="transaction_case",
        resource_id=str(case_id),
        payload={
            "evidence": {"observed_at": "2026-09-14T20:00:00Z", "source_refresh": {"total": "1.01"}},
            "correction_candidate": {"must_not_return": True},
        },
    )
    params = {"observation_id": str(event.id), "section": "source"}
    result = await read_observation(db, tenant_a.id, None, case_id, params, "test")
    assert result["evidence"] == {"total": "1.01"}
    assert result["native_api_calls"] == 0 and "Historical" in result["authority"]
    assert "correction_candidate" not in result
    with pytest.raises(ValueError, match="unavailable"):
        await read_observation(db, tenant_b.id, None, case_id, params, "test")
    with pytest.raises(ValueError, match="unavailable"):
        await read_observation(db, tenant_a.id, None, uuid4(), params, "test")
    # A caller cannot select an unrelated audit payload by guessing its ID.
    wrong = await log_event(
        db,
        tenant_a.id,
        category="chat",
        action="chat.turn",
        resource_type="transaction_case",
        resource_id=str(case_id),
        payload={"evidence": {"source_refresh": "private"}},
    )
    with pytest.raises(ValueError, match="unavailable"):
        await read_observation(db, tenant_a.id, None, case_id, {**params, "observation_id": str(wrong.id)}, "test")


@pytest.mark.parametrize("kind", ["tax", "credit", "discount", "sales_order"])
def test_triage_never_skips_evidence_for_any_supported_correction(kind):
    from app.services.transaction_ops.group_investigation import unsupported_source_recipe
    from tests.test_accounting_approval_flow import kind_proposal

    proposal = kind_proposal(kind)
    assert proposal is not None
    assert unsupported_source_recipe(proposal["source"]) is False


@pytest.mark.parametrize("kind", ["tax", "credit", "discount", "sales_order"])
def test_deferred_source_basis_is_ineligible_under_every_current_adapter(kind):
    from app.services.transaction_ops.group_investigation import unsupported_source_recipe
    from app.services.transaction_ops.sales_credit import build_candidate as credit_candidate
    from app.services.transaction_ops.sales_order_alignment import build_candidate as order_candidate
    from app.services.transaction_ops.tax_correction import candidate as tax_candidate
    from tests.test_invoice_discount import unpaid_inputs
    from tests.test_sales_credit import inputs
    from tests.test_sales_order_alignment import inputs as order_inputs
    from tests.test_tax_correction import fixture

    if kind == "tax":
        e, r, v, source = fixture()
        source.update(adjustments=[], included_tax_total="0", additional_tax_total="1")
        assert unsupported_source_recipe(source)
        assert tax_candidate(e, r, v, source) is None
    else:
        data = inputs() if kind == "credit" else unpaid_inputs() if kind == "discount" else order_inputs()
        data["source"].update(adjustments=[], included_tax_total="0", additional_tax_total="1")
        assert unsupported_source_recipe(data["source"])
        assert (order_candidate if kind == "sales_order" else credit_candidate)(**data) is None


@pytest.mark.parametrize(
    "source", [{}, {"adjustments": []}, {"adjustments": [], "included_tax_total": "bad", "additional_tax_total": "2"}]
)
def test_unknown_source_basis_keeps_full_evidence(source):
    from app.services.transaction_ops.group_investigation import unsupported_source_recipe

    assert unsupported_source_recipe(source) is False


@pytest.mark.parametrize("kind", ["tax", "credit", "discount", "sales_order", "unsupported"])
async def test_actual_evidence_tool_uses_full_reads_for_supported_shapes_and_refreshes_source_once(kind, monkeypatch):
    from copy import deepcopy

    from app.mcp.tools import transaction_ops_tools as tool
    from tests.test_accounting_approval_flow import kind_proposal
    from tests.test_tax_correction import fixture

    e, report, review, source = fixture()
    proposal = kind_proposal(kind if kind != "unsupported" else "tax")
    source = deepcopy(proposal["source"])
    if kind == "unsupported":
        source.update(adjustments=[], included_tax_total="0", additional_tax_total="1")
    db = AsyncMock(spec=AsyncSession)
    db.info = {}
    db.scalar.return_value = None
    case = SimpleNamespace(id=uuid4(), scope_json={}, latest_report_json=report, order_reference=source["number"])
    actor, tenant = SimpleNamespace(id=uuid4()), uuid4()
    monkeypatch.setattr(tool, "_authorize", AsyncMock(return_value=(db, tenant, actor)))
    monkeypatch.setattr("app.services.transaction_ops.case_service.get_case", AsyncMock(return_value=case))
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=review)
    )
    collect = AsyncMock(return_value={**deepcopy(e), "blockers": [], "assessment": {}})
    refresh = AsyncMock(return_value=source)
    monkeypatch.setattr("app.services.transaction_ops.accounting_evidence.collect_accounting_evidence", collect)
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.refresh_source", refresh)
    monkeypatch.setattr("app.services.transaction_ops.commercial_credits.collect_commercial_credits", AsyncMock())
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.candidate", lambda *args: None)
    monkeypatch.setattr(
        "app.services.transaction_ops.resolution_assessment.reference_provenance", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.services.audit_service.log_event", AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    result = await tool.execute_accounting_evidence({"case_id": str(case.id)}, context={"group_preparation": True})
    assert result["success"], result
    assert refresh.await_count == 1
    assert collect.await_args.kwargs == ({"posting_detail": False} if kind == "unsupported" else {})
    assert result["accounting_evidence"]["source_refresh"] == source


def test_representative_reads_bound_work_and_preserve_exact_case_observation_pairs():
    from app.services.transaction_ops.group_investigation import representative_reads

    batches = [{"orders": [{"case_id": str(i), "audit_id": "audit-" + str(i)}]} for i in range(55)]
    reads = representative_reads({"batches": batches}, [{"name": "transaction_ops_accounting_evidence"}])
    assert len(reads) == 8
    assert {p["case_id"] for p in reads} == {"0", "1", "2", "3"}
    assert all(p["observation_id"] == "audit-" + p["case_id"] for p in reads)
    assert {p["section"] for p in reads} == {"source", "documents"}
