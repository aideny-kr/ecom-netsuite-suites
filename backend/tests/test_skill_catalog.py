"""Product catalog readiness must be scoped, non-executing and conservative."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.skills import get_all_skills_metadata
from app.services.skill_catalog import _BINDINGS, resolve_catalog


def catalog(tools=(), permissions=()):
    return {s.slug: s for s in resolve_catalog(get_all_skills_metadata(), list(tools), set(permissions))}


def local(*names):
    return [{"name": n} for n in names]


def mb(connector, *names, tag="metabase_mcp"):
    return [{"name": f"ext__{connector.hex}__{n}", "description": f"[{tag}] private account label"} for n in names]


def test_all_current_skills_have_reviewed_bindings_and_no_schedule_claim():
    result = catalog()
    assert set(result) == set(_BINDINGS)
    for s in result.values():
        assert s.kind == "expertise"
        assert s.execution_surfaces[1].status == "unsupported"
        assert s.execution_surfaces[1].blockers[0].code == "no_execution_binding"
        assert len(s.version) == 64
        assert "_path" not in s.model_dump()
        assert not s.provenance.startswith("/")


def test_missing_financial_permission_and_disconnected_tools_are_distinct():
    result = catalog(local("netsuite_financial_report"))
    blocked = result["pl_flux_variance"].execution_surfaces[0]
    assert blocked.status == "blocked"
    assert [b.code for b in blocked.blockers] == ["permission_required"]
    assert (
        catalog(local("netsuite_financial_report"), ["chat.financial_reports"])["pl_flux_variance"]
        .execution_surfaces[0]
        .status
        == "available"
    )
    assert (
        catalog(permissions=["chat.financial_reports"])["pl_flux_variance"].execution_surfaces[0].blockers[0].code
        == "tool_unavailable"
    )


def test_metabase_requires_matching_source_tag_and_same_connector():
    a, b = uuid.uuid4(), uuid.uuid4()
    for tools in [
        mb(a, "query", "search", "read_resource", tag="custom"),
        mb(a, "query") + mb(b, "search", "read_resource"),
        mb(a, "execute_query", "search", "read_resource"),
    ]:
        assert catalog(tools)["metabase_bi"].execution_surfaces[0].status == "blocked"
    for tools in [
        mb(a, "query", "search", "read_resource"),
        mb(a, "construct_query", "execute_query", "search", "read_resource"),
    ]:
        s = catalog(tools)["metabase_bi"]
        assert s.execution_surfaces[0].status == "available"
        assert a.hex not in s.model_dump_json()
        assert "private account" not in s.model_dump_json()


def test_transaction_evidence_requires_actual_tool_and_permission():
    tools = local("transaction_ops_accounting_evidence")
    assert catalog(tools)["accounting_operations"].execution_surfaces[0].status == "blocked"
    assert catalog(tools, ["connections.view"])["accounting_operations"].execution_surfaces[0].status == "available"


def test_unregistered_markdown_fails_closed_and_version_tracks_content(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("initial")
    meta = dict(name="New", description="New skill", triggers=["/new"], slug="new", _path=str(p))
    a = resolve_catalog([meta], [], set())[0]
    assert a.execution_surfaces[0].blockers[0].code == "binding_missing"
    p.write_text("changed")
    assert resolve_catalog([meta], [], set())[0].version != a.version


@pytest.mark.asyncio
async def test_catalog_api_is_user_scoped_and_refreshes_revoked_readiness(client, db, admin_user):
    user, headers = admin_user
    with (
        patch("app.services.chat.tools.build_all_tool_definitions", new_callable=AsyncMock) as build,
        patch("app.services.chat.orchestrator._check_connection_health", new_callable=AsyncMock, return_value=[]),
    ):
        build.return_value = local("netsuite_suiteql")
        response = await client.get("/api/v1/skills/catalog", headers=headers)
        assert response.status_code == 200
        assert build.call_args.args[1] == user.tenant_id
        initial = next(s for s in response.json() if s["slug"] == "inventory_check")
        assert initial["execution_surfaces"][0]["status"] == "available"
        build.return_value = []
        response = await client.get("/api/v1/skills/catalog", headers=headers)
        revoked = next(s for s in response.json() if s["slug"] == "inventory_check")
        assert revoked["execution_surfaces"][0]["status"] == "blocked"
        assert revoked["version"] == initial["version"]


@pytest.mark.asyncio
async def test_real_inventory_filters_foreign_disabled_and_inactive_connectors(client, db, admin_user):
    from app.models.mcp_connector import McpConnector
    from app.models.tenant import Tenant

    user, headers = admin_user
    other = Tenant(name="Other catalog company", slug=f"other-catalog-{uuid.uuid4().hex[:8]}")
    db.add(other)
    await db.flush()
    tool_defs = [{"name": n, "input_schema": {"type": "object"}} for n in ("query", "search", "read_resource")]
    own = McpConnector(
        tenant_id=user.tenant_id,
        provider="metabase_mcp",
        label="PRIVATE-CATALOG-LABEL",
        server_url="https://example.invalid/mcp",
        auth_type="none",
        status="active",
        is_enabled=False,
        discovered_tools=tool_defs,
    )
    foreign = McpConnector(
        tenant_id=other.id,
        provider="metabase_mcp",
        label="FOREIGN-CATALOG-LABEL",
        server_url="https://example.invalid/other",
        auth_type="none",
        status="active",
        is_enabled=True,
        discovered_tools=tool_defs,
    )
    db.add_all([own, foreign])
    await db.flush()

    async def state():
        response = await client.get("/api/v1/skills/catalog", headers=headers)
        assert response.status_code == 200
        assert "PRIVATE-CATALOG-LABEL" not in response.text
        assert "FOREIGN-CATALOG-LABEL" not in response.text
        assert str(own.id) not in response.text and own.id.hex not in response.text
        return next(s for s in response.json() if s["slug"] == "metabase_bi")["execution_surfaces"][0]["status"]

    assert await state() == "blocked"
    own.is_enabled = True
    await db.flush()
    assert await state() == "available"
    own.status = "revoked"
    await db.flush()
    assert await state() == "blocked"


@pytest.mark.asyncio
async def test_active_company_policy_blocks_catalog_readiness(client, db, admin_user):
    from app.models.policy_profile import PolicyProfile

    user, headers = admin_user
    policy = PolicyProfile(
        tenant_id=user.tenant_id, name="Restricted tools", tool_allowlist=["workspace_list_files"], is_active=True
    )
    db.add(policy)
    await db.flush()
    with patch(
        "app.services.chat.tools.build_all_tool_definitions",
        new_callable=AsyncMock,
        return_value=local("netsuite_suiteql"),
    ):
        response = await client.get("/api/v1/skills/catalog", headers=headers)
        assert response.status_code == 200
        card = next(s for s in response.json() if s["slug"] == "inventory_check")
        assert card["execution_surfaces"][0]["status"] == "blocked"
        assert card["execution_surfaces"][0]["blockers"][0]["code"] == "policy_denied"
        assert "Restricted tools" not in response.text
        policy.tool_allowlist = ["netsuite_suiteql"]
        await db.flush()
        response = await client.get("/api/v1/skills/catalog", headers=headers)
        assert (
            next(s for s in response.json() if s["slug"] == "inventory_check")["execution_surfaces"][0]["status"]
            == "available"
        )


@pytest.mark.asyncio
async def test_disabled_feature_blocks_accounting_even_with_tools_and_permission(client, db, admin_user):
    from sqlalchemy import update

    from app.models.feature_flag import TenantFeatureFlag
    from app.services.feature_flag_service import clear_cache

    user, headers = admin_user
    await db.execute(
        update(TenantFeatureFlag)
        .where(TenantFeatureFlag.tenant_id == user.tenant_id, TenantFeatureFlag.flag_key == "reconciliation")
        .values(enabled=False)
    )
    clear_cache()
    with (
        patch(
            "app.services.chat.tools.build_all_tool_definitions",
            new_callable=AsyncMock,
            return_value=local("transaction_ops_accounting_evidence"),
        ),
        patch("app.core.dependencies.has_permission", new_callable=AsyncMock, return_value=True),
    ):
        response = await client.get("/api/v1/skills/catalog", headers=headers)
        assert response.status_code == 200
        card = next(s for s in response.json() if s["slug"] == "accounting_operations")
        assert card["execution_surfaces"][0]["status"] == "blocked"
        assert card["execution_surfaces"][0]["blockers"][0]["code"] == "feature_disabled"
    clear_cache()
