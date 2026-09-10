"""Investigation-only chat tools: tenant checks, durable enqueue, deterministic evidence."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.mcp.tools import transaction_ops_tools as mod

TENANT, ACTOR, CONFIG, RUN = (uuid.uuid4() for _ in range(4))
ORDER = "R123456789"


@pytest.fixture
def ctx(monkeypatch):
    db = AsyncMock()
    db.add = MagicMock()
    actor = SimpleNamespace(id=ACTOR, tenant_id=TENANT, actor_type="user", is_active=True)
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = actor
    monkeypatch.setattr(mod, "has_permission", AsyncMock(return_value=True))
    monkeypatch.setattr(mod.feature_flag_service, "is_enabled", AsyncMock(return_value=True))
    return {
        "db": db,
        "tenant_id": str(TENANT),
        "actor_id": str(ACTOR),
        "correlation_id": "server-correlation",
        "conversation_id": "server-conversation",
    }


@pytest.fixture
def state(monkeypatch):
    state = SimpleNamespace(
        list_configs=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=CONFIG,
                    name="Production EUR orders",
                    enabled=True,
                    schedule_enabled=False,
                    subsidiary_id="3",
                    record_type="salesorder",
                    mapping_json={"secret": "secret"},
                )
            ]
        ),
        create_run=AsyncMock(return_value=SimpleNamespace(id=RUN, status="pending")),
        get_run=AsyncMock(return_value=SimpleNamespace(id=RUN, status="finished", termination_reason="done")),
        list_findings=AsyncMock(return_value=[]),
        list_proposals=AsyncMock(return_value=[]),
    )

    class Request:
        def __init__(self, **values):
            self.__dict__.update(values)

    monkeypatch.setattr(mod, "_state_dependencies", lambda: (state, Request))
    return state


async def test_group_preparation_preserves_scope_and_collects_every_membership_page(ctx, monkeypatch):
    ctx["db"].info = {}
    scope = {"group_id": "a" * 32, "review_run_ids": [str(uuid.uuid4())], "status": "needs_review", "search": "R"}
    members = [{"case_id": str(uuid.uuid4()), "order_reference": f"R{i}"} for i in range(53)]
    get_members = AsyncMock(
        side_effect=[{"cases": members[:50], "has_next": True}, {"cases": members[50:], "has_next": False}]
    )
    monkeypatch.setattr("app.services.transaction_ops.case_groups.group_members", get_members)
    result = await mod.execute_accounting_group(scope, context=ctx)
    assert result["case_count"] == 53 and result["financial_writes"] == 0
    assert ctx["db"].info["accounting_group_selection"]["members"] == members
    for index, call in enumerate(get_members.await_args_list):
        assert call.args[1] == TENANT
        assert call.kwargs == {**scope, "limit": 50, "offset": index * 50}


async def test_group_preparation_checks_permission_before_loading_any_cases(ctx, monkeypatch):
    members = AsyncMock()
    monkeypatch.setattr(mod, "has_permission", AsyncMock(return_value=False))
    monkeypatch.setattr("app.services.transaction_ops.case_groups.group_members", members)
    result = await mod.execute_accounting_group({"group_id": "a" * 32}, context=ctx)
    assert result["success"] is False
    members.assert_not_awaited()


@pytest.mark.parametrize("flags", [{"celigo": False, "reconciliation": True}, {"celigo": True}, {}])
async def test_financial_authorization_does_not_use_cached_enabled_flags(ctx, monkeypatch, flags):
    cached = AsyncMock(return_value=True)
    monkeypatch.setattr(mod.feature_flag_service, "is_enabled", cached)
    monkeypatch.setattr(mod.feature_flag_service, "get_all_flags", AsyncMock(return_value=flags))
    with pytest.raises(mod._ToolError, match="feature_disabled"):
        await mod._authorize(ctx, create=True, fresh=True)
    cached.assert_not_awaited()


async def test_group_tool_rejects_model_supplied_approval_or_member_payloads(ctx):
    for extra in ({"human_approved": True}, {"case_ids": [str(uuid.uuid4())]}, {"tool_input": {"amount": 999}}):
        result = await mod.execute_accounting_group({"group_id": "a" * 32, **extra}, context=ctx)
        assert result["success"] is False


async def test_configs_project_scope_without_mapping_payload(ctx, state):
    result = await mod.execute_configs({}, context=ctx)
    assert result["configs"][0]["config_id"] == str(CONFIG)
    assert "secret" not in json.dumps(result)
    state.list_configs.assert_awaited_once_with(ctx["db"], TENANT)
    sql = str(ctx["db"].execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "users.tenant_id =" in sql and "users.is_active IS true" in sql
    assert "tenants.is_active IS true" in sql


@pytest.mark.parametrize("permission", ["connections.view", "recon.run"])
async def test_run_permission_gate_is_inside_tool(ctx, state, monkeypatch, permission):
    monkeypatch.setattr(mod, "has_permission", AsyncMock(side_effect=lambda db, actor, name: name != permission))
    result = await mod.execute_run({"config_id": str(CONFIG), "order_references": [ORDER]}, context=ctx)
    assert result["error"] == "permission_denied"
    state.create_run.assert_not_awaited()


@pytest.mark.parametrize("flag", ["celigo", "reconciliation"])
async def test_flags_gate_all_tools(ctx, state, monkeypatch, flag):
    monkeypatch.setattr(
        mod.feature_flag_service, "is_enabled", AsyncMock(side_effect=lambda db, tenant, key: key != flag)
    )
    for fn, params in [
        (mod.execute_configs, {}),
        (mod.execute_run, {"config_id": str(CONFIG), "order_references": [ORDER]}),
        (mod.execute_status, {"run_id": str(RUN)}),
    ]:
        assert (await fn(params, context=ctx))["error"] == "feature_disabled"
    state.create_run.assert_not_awaited()
    state.list_configs.assert_not_awaited()
    state.get_run.assert_not_awaited()


async def test_missing_or_cross_tenant_actor_cannot_read(ctx, state):
    ctx["db"].execute.return_value.scalar_one_or_none.return_value = None
    assert (await mod.execute_configs({}, context=ctx))["error"] == "actor_unavailable"
    state.list_configs.assert_not_awaited()


async def test_creation_is_durable_before_queue_and_never_accepts_approval(ctx, state, monkeypatch):
    events = []

    async def create(*args, **kwargs):
        events.append("committed")  # state_service's documented create_run contract
        request = args[3]
        assert request.origin == "chat"
        assert "server" not in request.evaluation_key  # fixed-length digest, no raw context
        assert not hasattr(request, "approved")
        assert kwargs["actor"].id == ACTOR
        return SimpleNamespace(id=RUN, status="pending")

    state.create_run.side_effect = create

    def send(name, **kwargs):
        assert events == ["committed"]
        assert name == "tasks.transaction_ops_run"
        assert kwargs["kwargs"] == {"tenant_id": str(TENANT), "run_id": str(RUN)}
        assert kwargs["connection"].connect_timeout == 1
        assert kwargs["connection"].transport_options["socket_timeout"] == 1
        assert kwargs["retry"] is False and kwargs["ignore_result"] is True
        events.append("sent")

    monkeypatch.setattr(mod.celery_app, "send_task", send)
    result = await mod.execute_run({"config_id": str(CONFIG), "order_references": [ORDER]}, context=ctx)
    assert events == ["committed", "sent"]
    assert result["dispatch_status"] == "queued"
    assert result["review_url"] == f"/transaction-operations/runs/{RUN}"


async def test_broker_failure_keeps_committed_pending_run_for_beat(ctx, state, monkeypatch):
    monkeypatch.setattr(mod.celery_app, "send_task", MagicMock(side_effect=ConnectionError("secret broker URL")))
    result = await mod.execute_run({"config_id": str(CONFIG), "order_references": [ORDER]}, context=ctx)
    assert result["run_id"] == str(RUN)
    assert result["dispatch_status"] == "pending_scheduler"
    assert "secret" not in json.dumps(result)
    ctx["db"].rollback.assert_not_awaited()


async def test_direct_tool_cannot_supply_evaluation_key_or_approval(ctx, state):
    params = {"config_id": str(CONFIG), "order_references": [ORDER], "evaluation_key": "model", "approved": True}
    assert (await mod.execute_run(params, context=ctx))["error"] == "invalid_parameters"
    state.create_run.assert_not_awaited()


async def test_status_returns_projected_decimal_difference_rows(ctx, state):
    state.list_findings.return_value = [
        SimpleNamespace(
            report_json={
                "order_reference": ORDER,
                "source": {"currency": "EUR", "secret": "secret"},
                "comparison": {
                    "recommended_action": "propose_amount_correction",
                    "currency": "EUR",
                    "differences": [
                        {"field": "total", "source": "10.0100000001", "target": "10.00", "delta": "0.0100000001"}
                    ],
                },
                "request": {"token": "secret"},
            }
        )
    ]
    result = await mod.execute_status({"run_id": str(RUN)}, context=ctx)
    assert result["suppress_llm_value"] is True
    assert "secret" not in json.dumps(result)
    assert "10.0100000001" in json.dumps(result["rows"])
    assert "EUR" in json.dumps(result["rows"])
    assert "propose_amount_correction" in json.dumps(result["rows"])
    assert result["query"] == ""


async def test_real_chat_dispatch_retains_permission_gate_and_strips_model_identity(ctx, state, monkeypatch):
    from app.services.chat.tools import execute_tool_call

    monkeypatch.setattr("app.mcp.governance.check_rate_limit", lambda *args: True)
    monkeypatch.setattr("app.mcp.governance.audit_service.log_event", AsyncMock())
    monkeypatch.setattr(mod.celery_app, "send_task", MagicMock())
    params = {"config_id": str(CONFIG), "order_references": [ORDER], "evaluation_key": "model", "approved": True}
    result = json.loads(
        await execute_tool_call("transaction_ops_run", params, TENANT, ACTOR, "server-correlation", ctx["db"])
    )
    assert result["run_id"] == str(RUN)
    request = state.create_run.call_args.args[3]
    assert request.evaluation_key != "model"
    monkeypatch.setattr(mod, "has_permission", AsyncMock(return_value=False))
    denied = json.loads(
        await execute_tool_call("transaction_ops_run", params, TENANT, ACTOR, "server-correlation", ctx["db"])
    )
    assert denied["error"] == "permission_denied"


def test_status_interceptor_withholds_values_but_emits_full_table():
    from app.services.chat.orchestrator import _intercept_tool_result

    raw = json.dumps(
        {
            "success": True,
            "run_id": str(RUN),
            "status": "finished",
            "columns": ["currency", "delta"],
            "rows": [["EUR", "0.0100000001"]],
            "row_count": 1,
            "suppress_llm_value": True,
            "review_url": f"/transaction-operations/runs/{RUN}",
            "query": "",
            "truncated": False,
        }
    )
    event, payload, condensed = _intercept_tool_result("transaction_ops_status", raw, result_id="r1")
    assert event == "data_table"
    assert payload["rows"] == [["EUR", "0.0100000001"]]
    assert "0.0100000001" not in condensed
    assert "1-row metric" not in condensed
    assert json.loads(condensed)["review_url"] == f"/transaction-operations/runs/{RUN}"


async def test_status_rejects_binary_float_evidence_instead_of_hiding_it(ctx, state):
    state.list_findings.return_value = [
        SimpleNamespace(
            report_json={
                "order_reference": ORDER,
                "source": {"currency": "EUR"},
                "comparison": {
                    "recommended_action": "human_review",
                    "differences": [{"field": "total", "source": 10.01, "target": "10.00", "delta": "0.01"}],
                },
            }
        )
    ]
    result = await mod.execute_status({"run_id": str(RUN)}, context=ctx)
    assert result["error"] == "invalid_stored_evidence"


async def test_status_discloses_table_row_cap_and_checks_findings_sentinel(ctx, state):
    finding = SimpleNamespace(
        report_json={
            "order_reference": ORDER,
            "comparison": {
                "recommended_action": "human_review",
                "currency": "EUR",
                "differences": [{"field": "total", "source": "1.00", "target": "0.00", "delta": "1.00"}] * 6,
            },
        }
    )
    state.list_findings.side_effect = [[finding] * 100, []]
    result = await mod.execute_status({"run_id": str(RUN)}, context=ctx)
    assert len(result["rows"]) == 500
    assert result["truncated"] is True
    assert state.list_findings.call_args.kwargs == {"offset": 100, "limit": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "rows": [["123.456789"]], "error": "unexpected"},
        {"success": True, "rows": "123.456789", "columns": []},
    ],
)
def test_malformed_status_result_never_falls_back_to_raw_amounts(payload):
    from app.services.chat.orchestrator import _intercept_tool_result

    event, rendered, condensed = _intercept_tool_result("transaction_ops_status", json.dumps(payload))
    assert event is None and rendered is None
    assert "123.456789" not in condensed


def test_transaction_status_does_not_pin_the_chat_to_netsuite():
    from app.services.chat.orchestrator import _compute_source_pin_update

    assert _compute_source_pin_update([{"tool": "transaction_ops_status"}]) == "leave_pin"


def test_nonstreaming_agent_also_withholds_investigation_amounts():
    from app.services.chat.agents.base_agent import _suppress_metric_value_for_llm

    raw = json.dumps(
        {
            "rows": [["EUR", "123.456789"]],
            "columns": ["currency", "delta"],
            "row_count": 1,
            "suppress_llm_value": True,
            "source_kind": "transaction_ops",
        }
    )
    assert "123.456789" not in _suppress_metric_value_for_llm(raw)


async def test_config_list_rejects_foreign_actor_returned_from_storage(ctx, state):
    ctx["db"].execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=ACTOR, tenant_id=uuid.uuid4(), actor_type="user"
    )
    assert (await mod.execute_configs({}, context=ctx))["error"] == "actor_unavailable"


async def test_new_family_enforces_real_deadline(ctx, state, monkeypatch):
    import asyncio

    async def slow(*args):
        await asyncio.sleep(1)
        return []

    state.list_configs.side_effect = slow
    monkeypatch.setattr(mod, "_TOOL_TIMEOUT", 0.01)
    result = await mod.execute_configs({}, context=ctx)
    assert result["error"] == "transaction_investigation_timeout"


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"order_references": ["../admin"]},
        {"order_references": [ORDER], "window_start": "2026-09-01T00:00:00Z", "window_end": "2026-09-02T00:00:00Z"},
        {"window_start": "2026-09-02T00:00:00Z", "window_end": "2026-09-01T00:00:00Z"},
    ],
)
async def test_actual_run_schema_rejects_invalid_scope_before_creation(ctx, state, monkeypatch, scope):
    from app.schemas.transaction_runs import RunCreate

    monkeypatch.setattr(mod, "_state_dependencies", lambda: (state, RunCreate))
    result = await mod.execute_run({"config_id": str(CONFIG), **scope}, context=ctx)
    assert result["error"] == "invalid_parameters"
    state.create_run.assert_not_awaited()


async def test_real_state_commits_before_broker_failure_and_retry_reuses_run(db, admin_user, monkeypatch):
    from app.services.transaction_ops import state_service
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config

    actor, _ = admin_user
    tenant_id, actor_id = actor.tenant_id, actor.id
    config = await seed_config(db, actor.tenant_id, actor)
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    events = []
    publications = []
    original_commit = db.commit

    async def observed_commit():
        await original_commit()
        events.append("committed")

    monkeypatch.setattr(db, "commit", observed_commit)

    def failed_publish(*args, **kwargs):
        publications.append({"committed_before": bool(events and events[-1] == "committed"), "retry": kwargs["retry"]})
        raise ConnectionError("private broker configuration")

    monkeypatch.setattr(mod.celery_app, "send_task", failed_publish)
    context = {
        "db": db,
        "tenant_id": str(actor.tenant_id),
        "actor_id": str(actor.id),
        "correlation_id": "stable-server-turn",
        "conversation_id": "conversation",
    }
    request = {"config_id": str(config.id), "order_references": [ORDER]}
    first = await mod.execute_run(request, context=context)
    assert first["dispatch_status"] == "pending_scheduler", first
    assert publications == [{"committed_before": True, "retry": False}]
    await db.rollback()
    persisted = await state_service.get_run(db, tenant_id, uuid.UUID(first["run_id"]))
    assert persisted.status == "pending"
    assert persisted.origin == "chat" and persisted.initiated_by == actor_id
    assert (await mod.execute_run(request, context=context))["run_id"] == first["run_id"]


async def test_real_dispatch_rejects_foreign_run(db, admin_user, admin_user_b, monkeypatch):
    from app.schemas.transaction_runs import RunCreate
    from app.services.chat.tools import execute_tool_call
    from app.services.transaction_ops import state_service
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config

    actor, _ = admin_user
    foreign_actor, _ = admin_user_b
    foreign_config = await seed_config(db, foreign_actor.tenant_id, foreign_actor)
    foreign_run = await state_service.create_run(
        db,
        foreign_actor.tenant_id,
        foreign_config.id,
        RunCreate(evaluation_key="foreign", order_references=[ORDER]),
        actor=foreign_actor,
    )
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    monkeypatch.setattr("app.mcp.governance.check_rate_limit", lambda *args: True)
    result = json.loads(
        await execute_tool_call(
            "transaction_ops_status",
            {"run_id": str(foreign_run.id)},
            actor.tenant_id,
            actor.id,
            "server-correlation",
            db,
        )
    )
    assert result["error"] == "not_found"
    assert "rows" not in result


async def test_status_can_read_a_case_with_bounded_history_and_exact_amounts(ctx, state, monkeypatch):
    from app.services.transaction_ops import case_service, resolution_history
    from tests.test_transaction_cases import NOW, report

    identifier = uuid.uuid4()
    evidence = report()
    evidence["comparison"]["currency"] = "USD"
    case = SimpleNamespace(
        id=identifier, status="open", order_reference=ORDER, latest_report_json=evidence, last_observed_at=NOW
    )
    loader = AsyncMock(return_value=case)
    monkeypatch.setattr(case_service, "get_case", loader)
    monkeypatch.setattr(case_service, "list_observations", AsyncMock(return_value=[]))
    history = {
        "resolutions": [{"proposal_id": str(RUN), "approved_by": str(ACTOR), "settlement_status": "succeeded"}],
        "examples": [],
        "usage": "A new approval is required",
        "truncated": False,
        "examples_truncated": False,
    }
    monkeypatch.setattr(resolution_history, "history", AsyncMock(return_value=history))
    result = await mod.execute_status({"case_id": str(identifier)}, context=ctx)
    assert result["success"] is True, result
    assert result["case_id"] == str(identifier) and result["row_count"] == 3
    assert result["rows"][0][-3:] == ["100.00", "99.00", "1.00"]
    assert result["suppress_llm_value"] is True
    assert result["resolution_history"] == history["resolutions"]
    assert result["investigation_guidance"]["executable"] is False
    from app.services.transaction_ops.chat_evidence import condense_status

    assert json.loads(condense_status(result))["resolution_history"] == history["resolutions"]
    assert json.loads(condense_status(result))["investigation_guidance"] == result["investigation_guidance"]
    loader.assert_awaited_once_with(ctx["db"], TENANT, identifier)
    bad = await mod.execute_status({"case_id": str(identifier), "run_id": str(RUN)}, context=ctx)
    assert bad["error"] == "invalid_parameters"


async def test_real_dispatch_reads_case_history_and_preserves_scope(db, admin_user, admin_user_b, monkeypatch):
    from app.services.chat.tools import execute_tool_call
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config
    from tests.test_transaction_resolution_history import case_and_proposal

    actor, foreign_actor = admin_user[0], admin_user_b[0]
    config = await seed_config(db, actor.tenant_id, actor)
    case, proposal = await case_and_proposal(db, actor, config, ORDER)
    for user in (actor, foreign_actor):
        for flag in ("celigo", "reconciliation"):
            await enable_feature_flag(db, user.tenant_id, flag)
    monkeypatch.setattr("app.mcp.governance.check_rate_limit", lambda *args: True)

    async def dispatch(user, params):
        return json.loads(
            await execute_tool_call("transaction_ops_status", params, user.tenant_id, user.id, "case-dispatch-test", db)
        )

    result = await dispatch(actor, {"case_id": str(case.id)})
    assert result["success"] is True, result
    assert result["case_id"] == str(case.id)
    assert result["investigation_guidance"]["kind"] == "investigation_guidance"
    assert result["resolution_history"][0]["proposal_id"] == str(proposal.id)
    assert result["resolution_history"][0]["requires_new_human_approval"] is True
    foreign = await dispatch(foreign_actor, {"case_id": str(case.id)})
    assert foreign["error"] == "not_found"
    assert "resolution_history" not in foreign
    assert "investigation_guidance" not in foreign
    mixed = await dispatch(actor, {"case_id": str(case.id), "run_id": str(proposal.run_id)})
    assert mixed["error"] == "invalid_parameters"


@pytest.mark.parametrize("blocked", ["permission", "actor", "case_scope", "extra_parameter"])
async def test_accounting_evidence_authorizes_before_native_reads(ctx, monkeypatch, blocked):
    from app.services.transaction_ops.state_service import StateError

    native = AsyncMock()
    get_case = AsyncMock(side_effect=StateError("case_not_found", 404))
    monkeypatch.setattr("app.services.transaction_ops.accounting_evidence.collect_accounting_evidence", native)
    monkeypatch.setattr("app.services.transaction_ops.case_service.get_case", get_case)
    params = {"case_id": str(uuid.uuid4())}
    if blocked == "permission":
        monkeypatch.setattr(mod, "has_permission", AsyncMock(return_value=False))
    elif blocked == "actor":
        ctx["db"].execute.return_value.scalar_one_or_none.return_value = None
    elif blocked == "extra_parameter":
        params["account_id"] = "other-account"
    result = await mod.execute_accounting_evidence(params, context=ctx)
    assert result["success"] is False
    native.assert_not_awaited()
    if blocked == "case_scope":
        assert get_case.await_args.args[1] == TENANT
