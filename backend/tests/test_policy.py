"""Tests for Policy Profiles — ~15 tests."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.policy_service import evaluate_tool_call, redact_output
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def max_tenant(db: AsyncSession):
    return await create_test_tenant(db, name="Max Corp", plan="max")


@pytest_asyncio.fixture
async def max_admin(db: AsyncSession, max_tenant):
    user, _ = await create_test_user(db, max_tenant, role_name="admin")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def max_readonly(db: AsyncSession, max_tenant):
    user, _ = await create_test_user(db, max_tenant, role_name="readonly")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def max_tenant_b(db: AsyncSession):
    return await create_test_tenant(db, name="Other Max Corp", plan="max")


@pytest_asyncio.fixture
async def max_admin_b(db: AsyncSession, max_tenant_b):
    user, _ = await create_test_user(db, max_tenant_b, role_name="admin")
    return user, make_auth_headers(user)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_policy(client: AsyncClient, max_admin):
    _, headers = max_admin
    resp = await client.post(
        "/api/v1/policies",
        json={"name": "Strict Read-Only", "read_only_mode": True, "max_rows_per_query": 500},
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Strict Read-Only"
    assert data["max_rows_per_query"] == 500


@pytest.mark.asyncio
async def test_policy_versions_increment_and_only_one_active(client: AsyncClient, max_admin):
    _, headers = max_admin
    resp1 = await client.post("/api/v1/policies", json={"name": "Policy A"}, headers=headers)
    resp2 = await client.post("/api/v1/policies", json={"name": "Policy B"}, headers=headers)
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["version"] == 1
    assert resp2.json()["version"] == 2

    list_resp = await client.get("/api/v1/policies", headers=headers)
    assert list_resp.status_code == 200
    policies = list_resp.json()
    active_count = sum(1 for p in policies if p["is_active"])
    assert active_count == 1
    assert any(p["name"] == "Policy B" and p["is_active"] for p in policies)


@pytest.mark.asyncio
async def test_list_policies(client: AsyncClient, max_admin):
    _, headers = max_admin
    await client.post("/api/v1/policies", json={"name": "Policy A"}, headers=headers)
    await client.post("/api/v1/policies", json={"name": "Policy B"}, headers=headers)

    resp = await client.get("/api/v1/policies", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_get_policy_by_id(client: AsyncClient, max_admin):
    _, headers = max_admin
    create_resp = await client.post("/api/v1/policies", json={"name": "Test"}, headers=headers)
    policy_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/policies/{policy_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test"


@pytest.mark.asyncio
async def test_update_policy(client: AsyncClient, max_admin):
    _, headers = max_admin
    create_resp = await client.post("/api/v1/policies", json={"name": "Original"}, headers=headers)
    policy_id = create_resp.json()["id"]

    update_resp = await client.put(
        f"/api/v1/policies/{policy_id}",
        json={"name": "Updated", "max_rows_per_query": 200},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Updated"
    assert update_resp.json()["max_rows_per_query"] == 200


@pytest.mark.asyncio
async def test_cannot_update_locked_policy(client: AsyncClient, max_admin):
    _, headers = max_admin
    create_resp = await client.post("/api/v1/policies", json={"name": "Locked", "is_locked": True}, headers=headers)
    policy_id = create_resp.json()["id"]

    update_resp = await client.put(
        f"/api/v1/policies/{policy_id}",
        json={"name": "Should Fail"},
        headers=headers,
    )
    assert update_resp.status_code == 409
    assert "locked" in update_resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delete_policy(client: AsyncClient, max_admin):
    _, headers = max_admin
    create_resp = await client.post("/api/v1/policies", json={"name": "To Delete"}, headers=headers)
    policy_id = create_resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/policies/{policy_id}", headers=headers)
    assert delete_resp.status_code == 204

    # Verify it's now inactive
    get_resp = await client.get(f"/api/v1/policies/{policy_id}", headers=headers)
    assert get_resp.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Policy Evaluation (unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_no_policy_allows_all():
    result = evaluate_tool_call(None, "ns_runSuiteQL", {"query": "SELECT * FROM transaction"})
    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_evaluate_blocked_fields():
    from types import SimpleNamespace

    policy = SimpleNamespace(
        blocked_fields=["salary", "ssn"],
        allowed_record_types=None,
        require_row_limit=False,
        max_rows_per_query=1000,
        tool_allowlist=None,
    )

    result = evaluate_tool_call(policy, "ns_runSuiteQL", {"query": "SELECT salary FROM employee"})
    assert result["allowed"] is False
    assert "salary" in result["reason"]


@pytest.mark.asyncio
async def test_evaluate_requires_row_limit():
    from types import SimpleNamespace

    policy = SimpleNamespace(
        blocked_fields=None,
        allowed_record_types=None,
        require_row_limit=True,
        max_rows_per_query=1000,
        tool_allowlist=None,
    )

    result = evaluate_tool_call(policy, "ns_runSuiteQL", {"query": "SELECT * FROM transaction"})
    assert result["allowed"] is False
    assert "row limit" in result["reason"]


@pytest.mark.asyncio
async def test_evaluate_with_row_limit_passes():
    from types import SimpleNamespace

    policy = SimpleNamespace(
        blocked_fields=None,
        allowed_record_types=None,
        require_row_limit=True,
        max_rows_per_query=1000,
        tool_allowlist=None,
    )

    result = evaluate_tool_call(policy, "ns_runSuiteQL", {"query": "SELECT * FROM transaction WHERE ROWNUM <= 10"})
    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_evaluate_tool_allowlist_blocks_disallowed_tool():
    from types import SimpleNamespace

    policy = SimpleNamespace(
        blocked_fields=None,
        allowed_record_types=None,
        require_row_limit=False,
        max_rows_per_query=1000,
        tool_allowlist=["netsuite.suiteql"],
    )

    result = evaluate_tool_call(policy, "workspace.propose_patch", {})
    assert result["allowed"] is False
    assert "not allowed" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Output Redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redact_output_strips_blocked_fields():
    from types import SimpleNamespace

    policy = SimpleNamespace(blocked_fields=["salary", "ssn"])

    result = redact_output(policy, {"name": "John", "salary": 100000, "ssn": "123-45-6789"})
    assert "name" in result
    assert "salary" not in result
    assert "ssn" not in result


@pytest.mark.asyncio
async def test_redact_output_no_policy():
    result = redact_output(None, {"name": "John", "salary": 100000})
    assert "salary" in result


@pytest.mark.asyncio
async def test_redact_output_list():
    from types import SimpleNamespace

    policy = SimpleNamespace(blocked_fields=["secret"])

    result = redact_output(policy, [{"name": "A", "secret": "x"}, {"name": "B", "secret": "y"}])
    assert len(result) == 2
    assert "secret" not in result[0]
    assert "secret" not in result[1]


# ---------------------------------------------------------------------------
# Tenant Isolation + RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_policies(client: AsyncClient, max_admin, max_admin_b):
    _, headers_a = max_admin
    _, headers_b = max_admin_b

    create_resp = await client.post("/api/v1/policies", json={"name": "A Policy"}, headers=headers_a)
    policy_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/policies/{policy_id}", headers=headers_b)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_readonly_cannot_create_policy(client: AsyncClient, max_readonly):
    _, headers = max_readonly
    resp = await client.post("/api/v1/policies", json={"name": "Not Allowed"}, headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pro_plan_cannot_use_policies(client: AsyncClient, db: AsyncSession):
    """Policies are max-plan only."""
    tenant = await create_test_tenant(db, name="Pro Corp", plan="pro")
    user, _ = await create_test_user(db, tenant, role_name="admin")
    headers = make_auth_headers(user)

    resp = await client.post("/api/v1/policies", json={"name": "Test"}, headers=headers)
    assert resp.status_code == 403
