"""Accounting references retain tenant/environment scope and never authorize posting."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.encryption import encrypt_credentials
from app.mcp.tools import netsuite_accounting_context as tool
from app.models.connection import Connection
from tests.conftest import create_test_tenant, create_test_user, enable_feature_flag


@pytest.fixture
async def ctx(db):
    tenant = await create_test_tenant(db)
    actor, _ = await create_test_user(db, tenant)
    connection = Connection(
        tenant_id=tenant.id,
        provider="netsuite",
        label="ERP",
        status="active",
        created_by=actor.id,
        metadata_json={"account_id": "12345"},
        encrypted_credentials=encrypt_credentials({"account_id": "12345", "secret": "never-expose-me"}),
    )
    db.add(connection)
    await db.flush()
    return {"db": db, "tenant_id": tenant.id, "actor_id": actor.id}, connection, tenant, actor


def reference(scope, rows=None, **extra):
    rows = rows if rows is not None else [["2", "EUR"]]
    return {
        "columns": ["id", "currency_code"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "verified_connection_scope": scope,
        **extra,
    }


def install_reader(monkeypatch):
    async def read(params, context):
        return reference(
            {"connection_id": params["connection_id"], "account_id": params["expected_account_id"]},
            limit=params["limit"],
        )

    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr(tool.netsuite_suiteql, "execute", reader)
    return reader


async def test_current_references_bind_scope_without_saving_knowledge(ctx, monkeypatch):
    context, connection, _, _ = ctx
    reader = install_reader(monkeypatch)
    result = await tool.execute({}, context)
    assert result["success"] and result["scope"]["account_id"] == "12345"
    assert result["sections"]["subsidiaries"]["rows"][0]["currency_code"] == "EUR"
    assert reader.await_count == 2
    for call in reader.await_args_list:
        assert call.args[0]["connection_id"] == str(connection.id)
        assert call.args[0]["expected_account_id"] == "12345"
        assert call.args[1]["actor_id"] == context["actor_id"]
    assert "never-expose-me" not in json.dumps(result)
    assert connection.metadata_json == {"account_id": "12345"}


@pytest.mark.parametrize(
    "denial", ["foreign_actor", "inactive_actor", "inactive_tenant", "service_actor", "no_permission"]
)
async def test_actor_denial_precedes_any_query(ctx, monkeypatch, denial):
    context, _, tenant, actor = ctx
    reader = install_reader(monkeypatch)
    if denial == "foreign_actor":
        other = await create_test_tenant(context["db"])
        other_actor, _ = await create_test_user(context["db"], other)
        context["actor_id"] = other_actor.id
    elif denial == "inactive_actor":
        actor.is_active = False
    elif denial == "inactive_tenant":
        tenant.is_active = False
    elif denial == "service_actor":
        actor.actor_type = "service"
    else:
        monkeypatch.setattr(tool, "has_permission", AsyncMock(return_value=False))
    await context["db"].flush()
    assert (await tool.execute({}, context))["error"] == "permission_denied"
    reader.assert_not_awaited()


@pytest.mark.parametrize("denial", ["foreign_connection", "revoked", "wrong_account", "wrong_environment"])
async def test_connection_scope_never_falls_back(ctx, monkeypatch, denial):
    context, connection, _, _ = ctx
    reader = install_reader(monkeypatch)
    params = {"connection_id": str(connection.id), "expected_account_id": "12345"}
    if denial == "foreign_connection":
        other = await create_test_tenant(context["db"])
        connection.tenant_id = other.id
    elif denial == "revoked":
        connection.status = "revoked"
    else:
        params["expected_account_id"] = "98765" if denial == "wrong_account" else "12345_SB1"
    await context["db"].flush()
    result = await tool.execute(params, context)
    assert result["error"] in {"connection_unavailable", "connection_scope_mismatch"}
    reader.assert_not_awaited()


async def test_multiple_connections_require_choice_then_use_only_selected(ctx, monkeypatch):
    context, _, tenant, actor = ctx
    other = Connection(
        tenant_id=tenant.id,
        provider="netsuite",
        label="Sandbox",
        status="active",
        created_by=actor.id,
        metadata_json={"account_id": "12345_SB1"},
        encrypted_credentials=encrypt_credentials({"account_id": "12345_SB1"}),
    )
    context["db"].add(other)
    await context["db"].flush()
    reader = install_reader(monkeypatch)
    result = await tool.execute({}, context)
    assert result["error"] == "connection_scope_required" and len(result["connections"]) == 2
    reader.assert_not_awaited()
    result = await tool.execute({"connection_id": str(other.id), "expected_account_id": "12345_SB1"}, context)
    assert result["scope"]["account_id"] == "12345-sb1"
    assert all(c.args[0]["connection_id"] == str(other.id) for c in reader.await_args_list)


@pytest.mark.parametrize(
    "params",
    [
        {"section": "execute"},
        {"query": "DELETE FROM account"},
        {"account_id": 1},
        {"section": "accounts", "account_id": "1 OR 1=1"},
        {"section": "accounts", "account_id": True},
        {"section": "periods", "calendar_year": 2200},
        {"section": "periods", "calendar_year": "2026"},
        {"expected_account_id": "12345"},
        {"connection_id": str(uuid4())},
        {"limit": 0},
        {"limit": 501},
    ],
)
async def test_invalid_or_executable_input_never_reaches_query(ctx, monkeypatch, params):
    context, _, _, _ = ctx
    reader = install_reader(monkeypatch)
    assert (await tool.execute(params, context))["error"] == "invalid_parameters"
    reader.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation", ["foreign_scope", "missing_scope", "bad_rows", "wrong_count", "error", "bad_limit"]
)
async def test_unverified_results_are_withheld(monkeypatch, mutation):
    scope = {"connection_id": str(uuid4()), "expected_account_id": "12345"}
    r = reference({"connection_id": scope["connection_id"], "account_id": "12345"})
    if mutation == "foreign_scope":
        r["verified_connection_scope"]["account_id"] = "12345-sb1"
    elif mutation == "missing_scope":
        del r["verified_connection_scope"]
    elif mutation == "bad_rows":
        r["rows"] = [["2"]]
    elif mutation == "wrong_count":
        r["row_count"] = 5
    elif mutation == "error":
        r["error"] = True
    else:
        r["limit"] = "100"
    monkeypatch.setattr(tool.netsuite_suiteql, "execute", AsyncMock(return_value=r))
    result = await tool._reference("SELECT id FROM subsidiary", scope, {}, 100)
    assert result["status"] == "unavailable" and "rows" not in result


@pytest.mark.parametrize(
    "rows,cap,truncated,expected",
    [([], 100, False, "complete"), ([["2", "EUR"]], 1, False, "partial"), ([["2", "EUR"]], 100, True, "partial")],
)
async def test_cap_equality_and_transport_truncation_are_partial(monkeypatch, rows, cap, truncated, expected):
    scope = {"connection_id": str(uuid4()), "expected_account_id": "12345"}
    r = reference({"connection_id": scope["connection_id"], "account_id": "12345"}, rows, truncated=truncated)
    monkeypatch.setattr(tool.netsuite_suiteql, "execute", AsyncMock(return_value=r))
    result = await tool._reference("SELECT id FROM subsidiary", scope, {}, cap)
    assert result["status"] == expected and "not proof" in result["coverage"]


async def test_period_dates_are_bounded_and_closed_does_not_mean_locked(ctx, monkeypatch):
    context, connection, _, _ = ctx
    reader = AsyncMock(
        return_value={
            "columns": ["closed", "alllocked"],
            "rows": [["F", "T"]],
            "row_count": 1,
            "verified_connection_scope": {"connection_id": str(connection.id), "account_id": "12345"},
        }
    )
    monkeypatch.setattr(tool.netsuite_suiteql, "execute", reader)
    result = await tool.execute({"section": "periods", "calendar_year": 2026}, context)
    assert result["sections"]["periods"]["rows"] == [{"closed": "F", "alllocked": "T"}]
    query = reader.await_args.args[0]["query"]
    assert "2026-01-01" in query and "2027-01-01" in query and "TO_CHAR" in query
    assert "not a fiscal-year definition" in result["limitations"]


async def test_account_lookup_reads_no_balances(ctx, monkeypatch):
    context, _, _, _ = ctx
    reader = install_reader(monkeypatch)
    await tool.execute({"section": "accounts", "account_id": 119}, context)
    query = reader.await_args.args[0]["query"]
    assert "WHERE id = 119" in query and "balance" not in query and "fullname" in query


async def test_policy_section_respects_accounting_feature_gate(ctx, monkeypatch):
    context, _, _, _ = ctx
    await enable_feature_flag(context["db"], context["tenant_id"], "reconciliation", False)
    reader = install_reader(monkeypatch)
    result = await tool.execute({"section": "policies"}, context)
    assert result["sections"]["policies"]["status"] == "unavailable"
    assert not result["success"] and result["error"] == "accounting_reference_unavailable"
    reader.assert_not_awaited()


async def test_all_failed_reads_report_failure_but_partial_overview_is_usable(ctx, monkeypatch):
    context, connection, _, _ = ctx
    ok = reference({"connection_id": str(connection.id), "account_id": "12345"})
    reader = AsyncMock(side_effect=[{"error": "unavailable"}, {"error": "unavailable"}, ok, {"error": "unavailable"}])
    monkeypatch.setattr(tool.netsuite_suiteql, "execute", reader)
    failed = await tool.execute({}, context)
    assert not failed["success"] and failed["error"] == "accounting_reference_unavailable"
    partial = await tool.execute({}, context)
    assert partial["success"] and partial["sections"]["books"]["status"] == "unavailable"
    assert partial["sections"]["subsidiaries"]["rows"]


@pytest.mark.parametrize(
    "month,ranges",
    [
        (4, ["April – June", "July – September", "October – December", "January – March"]),
        (12, ["December – February", "March – May", "June – August", "September – November"]),
    ],
)
def test_fiscal_quarters_use_correct_months_and_state_configuration(month, ranges):
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id="fiscal")
    agent._context = {"fiscal_year_start_month": month}
    agent._tool_defs = []
    prompt = agent.system_prompt
    assert "configured fiscal-year default" in prompt
    for quarter, dates in enumerate(ranges, 1):
        assert f"Fiscal Q{quarter} = {dates}" in prompt


def test_accounting_profile_activates_with_tool_and_current_facts_outrank_notes():
    from app.services.chat.agents.unified_agent import UnifiedAgent
    from app.services.chat.tools import build_local_tool_definitions

    agent = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id="knowledge")
    agent._tool_defs = [t for t in build_local_tool_definitions() if t["name"] == "netsuite_accounting_context"]
    agent._soul_quirks = "All subsidiaries always use USD."
    prompt = agent.system_prompt
    assert "Current NetSuite Accounting Context" in prompt
    assert "Current tool evidence takes precedence" in prompt and "HIGHEST PRIORITY" not in prompt
    assert "Missing configuration is not a company policy prohibition" in " ".join(prompt.split())
    agent._tool_defs = []
    assert "Current NetSuite Accounting Context" not in agent.system_prompt


async def test_policies_preserve_current_overrides_and_missing_subsidiary_mapping(db, admin_user, monkeypatch):
    from app.services.transaction_ops import accounting_profiles as profiles
    from app.services.transaction_ops import state_service as state
    from tests.test_accounting_profiles import setup
    from tests.test_transaction_ops_state import config_input

    actor = admin_user[0]
    config, connection, profile = await setup(db, actor, legacy=True)
    other = await state.create_config(
        db,
        actor.tenant_id,
        config_input(source_step_id=config.source_step_id, netsuite_connection_id=connection.id, subsidiary_id="2"),
        actor=actor,
    )
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag, True)
    context = {"db": db, "tenant_id": actor.tenant_id, "actor_id": actor.id}
    params = {"section": "policies", "connection_id": str(connection.id), "expected_account_id": "6738075_SB1"}
    reader = install_reader(monkeypatch)

    async def read():
        result = await tool.execute(params, context)
        assert result["sections"]["policies"]["status"] == "complete"
        return {t["config_id"]: t for t in result["sections"]["policies"]["configured_treatments"]}

    treatments = await read()
    assert treatments[str(config.id)]["sales_credit_profile"] == profile
    assert treatments[str(other.id)]["sales_credit_status"] == "not_configured"
    assert treatments[str(other.id)]["sales_credit_profile"] is None
    scope = profiles.config_scope(config)
    key = state.business_digest(scope)
    connection.metadata_json = {
        **connection.metadata_json,
        profiles.NAMESPACE: {key: {"schema_version": 1, "scope": scope, "sales_credit_profile": None}},
    }
    await db.flush()
    assert (await read())[str(config.id)]["sales_credit_status"] == "not_configured"
    connection.metadata_json = {**connection.metadata_json, profiles.NAMESPACE: {key: {"scope": scope}}}
    await db.flush()
    assert (await read())[str(config.id)]["sales_credit_status"] == "invalid_configuration"
    reader.assert_not_awaited()


async def test_connection_revoked_during_policy_read_is_unavailable(db, admin_user, monkeypatch):
    from app.services.transaction_ops import accounting_profiles as profiles
    from tests.test_accounting_profiles import setup

    actor = admin_user[0]
    _, connection, _ = await setup(db, actor, legacy=True)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag, True)
    original = profiles.sales_credit_profile

    async def revoke(*args):
        connection.status = "revoked"
        await db.flush()
        return await original(*args)

    monkeypatch.setattr(profiles, "sales_credit_profile", revoke)
    result = await tool.execute({"section": "policies"}, {"db": db, "tenant_id": actor.tenant_id, "actor_id": actor.id})
    assert result["sections"]["policies"]["status"] == "unavailable"
    assert "configured_treatments" not in result["sections"]["policies"]


@pytest.mark.parametrize("mode", ["rest", "mcp_only", "revoked", "discovery_failure"])
async def test_accounting_tool_requires_live_rest_connection(ctx, monkeypatch, mode):
    from types import SimpleNamespace

    from app.services.chat.tools import build_all_tool_definitions, build_discovery_fallback_tools

    context, connection, _, _ = ctx
    if mode == "revoked":
        connection.status = "revoked"
        await context["db"].flush()
    if mode == "mcp_only":
        connection.status = "revoked"
        await context["db"].flush()
    if mode == "discovery_failure":
        monkeypatch.setattr("app.services.connection_service.list_connections", AsyncMock(side_effect=RuntimeError))
    connectors = [SimpleNamespace(provider="netsuite_mcp")] if mode == "mcp_only" else []
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_active_connectors_for_tenant", AsyncMock(return_value=connectors)
    )
    monkeypatch.setattr("app.services.chat.tools.build_external_tool_definitions", lambda _: [])
    names = {t["name"] for t in await build_all_tool_definitions(context["db"], context["tenant_id"])}
    assert ("netsuite_accounting_context" in names) == (mode == "rest")
    assert "netsuite_accounting_context" not in {t["name"] for t in build_discovery_fallback_tools()}


async def test_chat_dispatch_preserves_authorization_and_governance(ctx, monkeypatch):
    from app.mcp import governance
    from app.services.chat.tools import execute_tool_call

    context, connection, _, _ = ctx
    reader = install_reader(monkeypatch)
    monkeypatch.setattr(governance, "check_rate_limit", lambda *args: True)
    params = {"section": "accounts", "account_id": 119}
    result = json.loads(
        await execute_tool_call(
            "netsuite_accounting_context",
            params,
            context["tenant_id"],
            context["actor_id"],
            "accounting-test",
            context["db"],
        )
    )
    assert result["success"] and result["scope"]["connection_id"] == str(connection.id)
    assert reader.await_count == 1
    assert reader.await_args.args[0]["limit"] == 100
    result = json.loads(
        await execute_tool_call(
            "netsuite_accounting_context", params, context["tenant_id"], uuid4(), "accounting-denied", context["db"]
        )
    )
    assert result["error"] == "permission_denied" and reader.await_count == 1


def test_shared_prompt_assembly_does_not_duplicate_accounting_context():
    from app.services.chat.orchestrator import _assemble_system_prompt

    defs = [{"name": "netsuite_accounting_context", "description": "Accounting reference", "input_schema": {}}]
    first = _assemble_system_prompt(template="{{TOOL_INVENTORY}}", tool_definitions=defs)
    second = _assemble_system_prompt(template=first, tool_definitions=defs)
    assert second.count("## Current NetSuite Accounting Context") == 1
