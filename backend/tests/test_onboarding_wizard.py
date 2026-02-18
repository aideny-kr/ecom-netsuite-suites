"""Tests for onboarding wizard checklist endpoints and service."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.mcp_connector import McpConnector
from app.models.policy_profile import PolicyProfile
from app.models.tenant import Tenant
from app.models.tenant_profile import TenantProfile
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceRun

BASE = "/api/v1/onboarding"


# ---- helpers ----


async def _create_confirmed_profile(db: AsyncSession, tenant: Tenant) -> TenantProfile:
    profile = TenantProfile(
        tenant_id=tenant.id,
        version=1,
        status="confirmed",
        industry="retail",
        business_description="Test business",
    )
    db.add(profile)
    await db.flush()
    return profile


async def _create_active_connection(db: AsyncSession, tenant: Tenant) -> Connection:
    conn = Connection(
        tenant_id=tenant.id,
        provider="netsuite",
        label="NS",
        status="active",
        encrypted_credentials="test",
    )
    db.add(conn)
    await db.flush()
    return conn


async def _create_active_mcp_connector(db: AsyncSession, tenant: Tenant) -> McpConnector:
    mcp = McpConnector(
        tenant_id=tenant.id,
        provider="netsuite_mcp",
        label="NetSuite MCP",
        server_url="https://mcp.example.com",
        auth_type="bearer",
        status="active",
        is_enabled=True,
    )
    db.add(mcp)
    await db.flush()
    return mcp


async def _create_active_policy(db: AsyncSession, tenant: Tenant, user: User) -> PolicyProfile:
    policy = PolicyProfile(
        tenant_id=tenant.id,
        name="Test Policy",
        is_active=True,
        created_by=user.id,
    )
    db.add(policy)
    await db.flush()
    return policy


async def _create_workspace(db: AsyncSession, tenant: Tenant, user: User) -> Workspace:
    ws = Workspace(tenant_id=tenant.id, name="Test WS", created_by=user.id)
    db.add(ws)
    await db.flush()
    return ws


async def _create_run(
    db: AsyncSession,
    tenant: Tenant,
    workspace: Workspace,
    user: User,
    run_type: str,
    status: str = "passed",
) -> WorkspaceRun:
    run = WorkspaceRun(
        tenant_id=tenant.id,
        workspace_id=workspace.id,
        run_type=run_type,
        status=status,
        triggered_by=user.id,
    )
    db.add(run)
    await db.flush()
    return run


# ---- tests ----


@pytest.mark.asyncio
async def test_get_checklist_creates_default_items(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession
):
    user, headers = admin_user
    resp = await client.get(f"{BASE}/checklist", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 5
    assert data["all_completed"] is False
    assert data["finalized_at"] is None
    step_keys = [i["step_key"] for i in data["items"]]
    assert step_keys == ["profile", "connection", "policy", "workspace", "first_success"]
    for item in data["items"]:
        assert item["status"] == "pending"


@pytest.mark.asyncio
async def test_complete_step_marks_completed(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    # Create confirmed profile so validation passes
    await _create_confirmed_profile(db, tenant_a)

    resp = await client.post(f"{BASE}/checklist/profile/complete", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["step_key"] == "profile"
    assert data["status"] == "completed"
    assert data["completed_at"] is not None
    assert data["completed_by"] == str(user.id)


@pytest.mark.asyncio
async def test_complete_step_emits_audit_event(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    await _create_confirmed_profile(db, tenant_a)
    await client.post(f"{BASE}/checklist/profile/complete", headers=headers)

    resp = await client.get(f"{BASE}/audit-trail", headers=headers)
    assert resp.status_code == 200
    events = resp.json()["events"]
    actions = [e["action"] for e in events]
    assert "onboarding.step_completed" in actions
    complete_event = next(e for e in events if e["action"] == "onboarding.step_completed")
    assert complete_event["correlation_id"] is not None


@pytest.mark.asyncio
async def test_complete_connection_requires_discovery_metadata(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    _, headers = admin_user
    await _create_active_connection(db, tenant_a)
    await _create_active_mcp_connector(db, tenant_a)
    resp = await client.post(f"{BASE}/checklist/connection/complete", headers=headers)
    assert resp.status_code == 400
    assert "completed discovery run" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_complete_connection_with_discovery_metadata(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    _, headers = admin_user
    await _create_active_connection(db, tenant_a)
    await _create_active_mcp_connector(db, tenant_a)
    resp = await client.post(
        f"{BASE}/checklist/connection/complete",
        headers=headers,
        json={"metadata": {"discovery_status": "completed", "summary": {"accounts_count": 10}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["metadata"]["discovery_status"] == "completed"


@pytest.mark.asyncio
async def test_skip_step_marks_skipped(client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession):
    user, headers = admin_user
    resp = await client.post(f"{BASE}/checklist/connection/skip", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["step_key"] == "connection"
    assert data["status"] == "skipped"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_validate_step_profile_no_profile(client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession):
    _, headers = admin_user
    resp = await client.get(f"{BASE}/checklist/profile/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "No confirmed tenant profile" in data["reason"]


@pytest.mark.asyncio
async def test_validate_step_profile_with_confirmed_profile(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    _, headers = admin_user
    await _create_confirmed_profile(db, tenant_a)
    resp = await client.get(f"{BASE}/checklist/profile/validate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_validate_connection_requires_both_mcp_and_oauth(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession
):
    """Neither MCP connector nor Connection exists → valid=False with both reasons."""
    _, headers = admin_user
    resp = await client.get(f"{BASE}/checklist/connection/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "No active NetSuite MCP connector" in data["reason"]
    assert "No active NetSuite OAuth connection" in data["reason"]


@pytest.mark.asyncio
async def test_validate_connection_mcp_only(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    """MCP connector exists but no Connection → valid=False."""
    _, headers = admin_user
    await _create_active_mcp_connector(db, tenant_a)
    resp = await client.get(f"{BASE}/checklist/connection/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "No active NetSuite OAuth connection" in data["reason"]
    assert "MCP connector" not in data["reason"]


@pytest.mark.asyncio
async def test_validate_connection_oauth_only(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    """Connection exists but no MCP connector → valid=False."""
    _, headers = admin_user
    await _create_active_connection(db, tenant_a)
    resp = await client.get(f"{BASE}/checklist/connection/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "No active NetSuite MCP connector" in data["reason"]
    assert "OAuth connection" not in data["reason"]


@pytest.mark.asyncio
async def test_validate_connection_both_exist(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    """Both MCP connector and Connection exist → valid=True."""
    _, headers = admin_user
    await _create_active_connection(db, tenant_a)
    await _create_active_mcp_connector(db, tenant_a)
    resp = await client.get(f"{BASE}/checklist/connection/validate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_validate_step_policy_no_policy(client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession):
    _, headers = admin_user
    resp = await client.get(f"{BASE}/checklist/policy/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "No active policy" in data["reason"]


@pytest.mark.asyncio
async def test_validate_step_policy_with_active_policy(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    await _create_active_policy(db, tenant_a, user)
    resp = await client.get(f"{BASE}/checklist/policy/validate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_validate_step_workspace_no_workspace(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession
):
    _, headers = admin_user
    resp = await client.get(f"{BASE}/checklist/workspace/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "No workspace" in data["reason"]


@pytest.mark.asyncio
async def test_validate_step_workspace_with_workspace(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    await _create_workspace(db, tenant_a, user)
    resp = await client.get(f"{BASE}/checklist/workspace/validate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_validate_step_first_success_requires_validate_and_unit_tests(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    workspace = await _create_workspace(db, tenant_a, user)
    await _create_run(db, tenant_a, workspace, user, run_type="sdf_validate", status="passed")
    resp = await client.get(f"{BASE}/checklist/first_success/validate", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "jest_unit_test" in data["reason"]


@pytest.mark.asyncio
async def test_validate_step_first_success_with_both_run_types(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    workspace = await _create_workspace(db, tenant_a, user)
    await _create_run(db, tenant_a, workspace, user, run_type="sdf_validate", status="passed")
    await _create_run(db, tenant_a, workspace, user, run_type="jest_unit_test", status="passed")
    resp = await client.get(f"{BASE}/checklist/first_success/validate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_setup_policy_creates_and_activates(client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession):
    _, headers = admin_user
    resp = await client.post(
        f"{BASE}/setup-policy",
        headers=headers,
        json={
            "read_only_mode": True,
            "sensitivity_default": "financial",
            "tool_allowlist": ["netsuite.suiteql", "workspace.read_file"],
            "max_rows_per_query": 500,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["step_key"] == "policy"
    assert data["status"] == "completed"
    assert data["metadata"] is not None
    assert "policy_id" in data["metadata"]

    from sqlalchemy import select

    policy_result = await db.execute(
        select(PolicyProfile).where(PolicyProfile.id == uuid.UUID(data["metadata"]["policy_id"]))
    )
    policy = policy_result.scalar_one_or_none()
    assert policy is not None
    assert policy.sensitivity_default == "financial"
    assert policy.tool_allowlist == ["netsuite.suiteql", "workspace.read_file"]


@pytest.mark.asyncio
async def test_setup_policy_defaults(client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession):
    _, headers = admin_user
    resp = await client.post(f"{BASE}/setup-policy", headers=headers, json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["step_key"] == "policy"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_sets_completed_at(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    # Complete required step: profile
    await _create_confirmed_profile(db, tenant_a)
    await _create_active_policy(db, tenant_a, user)
    await client.post(f"{BASE}/checklist/profile/complete", headers=headers)

    resp = await client.post(f"{BASE}/finalize", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["completed_at"] is not None

    from sqlalchemy import select

    policy_result = await db.execute(
        select(PolicyProfile).where(
            PolicyProfile.tenant_id == tenant_a.id,
            PolicyProfile.is_active.is_(True),
        )
    )
    policy = policy_result.scalar_one_or_none()
    assert policy is not None
    assert policy.is_locked is True


@pytest.mark.asyncio
async def test_finalize_emits_audit_event(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    tenant_a: Tenant,
    db: AsyncSession,
):
    user, headers = admin_user
    await _create_confirmed_profile(db, tenant_a)
    await client.post(f"{BASE}/checklist/profile/complete", headers=headers)
    await client.post(f"{BASE}/finalize", headers=headers)

    resp = await client.get(f"{BASE}/audit-trail", headers=headers)
    assert resp.status_code == 200
    events = resp.json()["events"]
    actions = [e["action"] for e in events]
    assert "onboarding.finalized" in actions


@pytest.mark.asyncio
async def test_finalize_fails_if_required_steps_incomplete(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession
):
    _, headers = admin_user
    resp = await client.post(f"{BASE}/finalize", headers=headers)
    assert resp.status_code == 400
    assert "profile" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_audit_trail_returns_onboarding_events(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    db: AsyncSession,
):
    _, headers = admin_user
    # Skip a step to generate an audit event
    await client.post(f"{BASE}/checklist/connection/skip", headers=headers)

    resp = await client.get(f"{BASE}/audit-trail", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) > 0
    assert data["events"][0]["action"] == "onboarding.step_skipped"


@pytest.mark.asyncio
async def test_tenant_b_cannot_see_tenant_a_checklist(
    client: AsyncClient,
    admin_user: tuple[User, dict],
    admin_user_b: tuple[User, dict],
    db: AsyncSession,
):
    user_a, headers_a = admin_user
    _, headers_b = admin_user_b

    # Tenant A skips a step
    await client.post(f"{BASE}/checklist/connection/skip", headers=headers_a)

    # Tenant B should see only pending items
    resp = await client.get(f"{BASE}/checklist", headers=headers_b)
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["status"] == "pending"


@pytest.mark.asyncio
async def test_checklist_requires_auth(client: AsyncClient):
    resp = await client.get(f"{BASE}/checklist")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_finalize_requires_auth(client: AsyncClient):
    resp = await client.post(f"{BASE}/finalize")
    assert resp.status_code in (401, 403)


# --- Two-phase connection authorize endpoints ---


@pytest.mark.asyncio
async def test_onboarding_mcp_authorize_endpoint(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession, monkeypatch
):
    """Onboarding MCP authorize endpoint returns authorize_url and state."""
    _, headers = admin_user

    # Mock Redis to avoid real connection
    class FakeRedis:
        async def setex(self, key, ttl, value):
            pass

        async def aclose(self):
            pass

    fake_module = type(
        "M",
        (),
        {
            "from_url": staticmethod(lambda *a, **kw: FakeRedis()),
        },
    )()
    monkeypatch.setattr("app.api.v1.onboarding.aioredis", fake_module)

    resp = await client.get(
        f"{BASE}/netsuite-mcp/authorize?account_id=12345_SB1&client_id=abc123",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "authorize_url" in data
    assert "state" in data
    assert "abc123" in data["authorize_url"]  # client_id appears in URL
    assert "system.netsuite.com" in data["authorize_url"]


@pytest.mark.asyncio
async def test_onboarding_mcp_authorize_requires_client_id(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession
):
    _, headers = admin_user
    resp = await client.get(
        f"{BASE}/netsuite-mcp/authorize?account_id=12345&client_id=",
        headers=headers,
    )
    assert resp.status_code == 400
    assert "client_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_onboarding_oauth_authorize_endpoint(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession, monkeypatch
):
    """Onboarding OAuth authorize endpoint returns authorize_url and state."""
    _, headers = admin_user

    # Mock Redis
    class FakeRedis:
        async def setex(self, key, ttl, value):
            pass

        async def aclose(self):
            pass

    fake_module = type(
        "M",
        (),
        {
            "from_url": staticmethod(lambda *a, **kw: FakeRedis()),
        },
    )()
    monkeypatch.setattr("app.api.v1.onboarding.aioredis", fake_module)

    # Ensure NETSUITE_OAUTH_CLIENT_ID is set
    from app.core.config import settings

    monkeypatch.setattr(settings, "NETSUITE_OAUTH_CLIENT_ID", "test-client-id")

    resp = await client.get(
        f"{BASE}/netsuite-oauth/authorize?account_id=12345_SB1",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "authorize_url" in data
    assert "state" in data


@pytest.mark.asyncio
async def test_onboarding_oauth_authorize_requires_account_id(
    client: AsyncClient, admin_user: tuple[User, dict], db: AsyncSession
):
    _, headers = admin_user
    resp = await client.get(
        f"{BASE}/netsuite-oauth/authorize?account_id=",
        headers=headers,
    )
    assert resp.status_code == 400
    assert "account_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_onboarding_authorize_requires_auth(client: AsyncClient):
    resp = await client.get(f"{BASE}/netsuite-mcp/authorize?account_id=123&client_id=abc")
    assert resp.status_code in (401, 403)
    resp2 = await client.get(f"{BASE}/netsuite-oauth/authorize?account_id=123")
    assert resp2.status_code in (401, 403)
