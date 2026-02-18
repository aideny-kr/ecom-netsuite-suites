"""Tests for AI Onboarding (tenant profiles) — ~20 tests."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pro_tenant(db: AsyncSession):
    return await create_test_tenant(db, name="Pro Corp", plan="pro")


@pytest_asyncio.fixture
async def pro_admin(db: AsyncSession, pro_tenant):
    user, _ = await create_test_user(db, pro_tenant, role_name="admin")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def pro_readonly(db: AsyncSession, pro_tenant):
    user, _ = await create_test_user(db, pro_tenant, role_name="readonly")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def pro_tenant_b(db: AsyncSession):
    return await create_test_tenant(db, name="Other Corp", plan="pro")


@pytest_asyncio.fixture
async def pro_admin_b(db: AsyncSession, pro_tenant_b):
    user, _ = await create_test_user(db, pro_tenant_b, role_name="admin")
    return user, make_auth_headers(user)


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_profile(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail", "business_description": "Online fashion retailer"},
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "draft"
    assert data["version"] == 1
    assert data["industry"] == "Retail"


@pytest.mark.asyncio
async def test_version_incrementing(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    resp1 = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=headers,
    )
    assert resp1.json()["version"] == 1

    resp2 = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Wholesale"},
        headers=headers,
    )
    assert resp2.json()["version"] == 2

    resp3 = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Manufacturing"},
        headers=headers,
    )
    assert resp3.json()["version"] == 3


@pytest.mark.asyncio
async def test_list_profiles(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    await client.post("/api/v1/onboarding/profiles", json={"industry": "Retail"}, headers=headers)
    await client.post("/api/v1/onboarding/profiles", json={"industry": "Wholesale"}, headers=headers)

    resp = await client.get("/api/v1/onboarding/profiles", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_get_profile_by_id(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Tech"},
        headers=headers,
    )
    profile_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/onboarding/profiles/{profile_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["industry"] == "Tech"


# ---------------------------------------------------------------------------
# Confirm + Prompt Template Generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_profile_generates_template(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={
            "industry": "E-commerce",
            "business_description": "Multi-brand online store",
            "netsuite_account_id": "12345",
        },
        headers=headers,
    )
    profile_id = create_resp.json()["id"]

    confirm_resp = await client.post(
        f"/api/v1/onboarding/profiles/{profile_id}/confirm",
        headers=headers,
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["status"] == "confirmed"
    assert confirm_resp.json()["confirmed_by"] is not None

    # Verify prompt template was generated
    template_resp = await client.get("/api/v1/onboarding/prompt-template", headers=headers)
    assert template_resp.status_code == 200
    template = template_resp.json()
    assert "E-commerce" in template["template_text"]
    assert template["is_active"] is True


@pytest.mark.asyncio
async def test_confirm_locks_version(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=headers,
    )
    profile_id = create_resp.json()["id"]

    # First confirm succeeds
    resp1 = await client.post(f"/api/v1/onboarding/profiles/{profile_id}/confirm", headers=headers)
    assert resp1.status_code == 200

    # Second confirm fails (already confirmed)
    resp2 = await client.post(f"/api/v1/onboarding/profiles/{profile_id}/confirm", headers=headers)
    assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_active_profile_returns_latest_confirmed(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    # Create and confirm v1
    r1 = await client.post("/api/v1/onboarding/profiles", json={"industry": "Retail"}, headers=headers)
    await client.post(f"/api/v1/onboarding/profiles/{r1.json()['id']}/confirm", headers=headers)

    # Create and confirm v2
    r2 = await client.post("/api/v1/onboarding/profiles", json={"industry": "Wholesale"}, headers=headers)
    await client.post(f"/api/v1/onboarding/profiles/{r2.json()['id']}/confirm", headers=headers)

    active = await client.get("/api/v1/onboarding/profiles/active", headers=headers)
    assert active.status_code == 200
    assert active.json()["industry"] == "Wholesale"
    assert active.json()["version"] == 2


@pytest.mark.asyncio
async def test_no_active_profile_returns_404(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    resp = await client.get("/api/v1/onboarding/profiles/active", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_profile_with_netsuite_metadata(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={
            "industry": "Retail",
            "chart_of_accounts": [
                {"number": "1000", "name": "Cash"},
                {"number": "4000", "name": "Revenue"},
            ],
            "subsidiaries": [{"name": "US Sub"}, {"name": "EU Sub"}],
            "suiteql_naming": {"transaction_type_field": "type", "date_field": "trandate"},
        },
        headers=headers,
    )
    profile_id = create_resp.json()["id"]
    await client.post(f"/api/v1/onboarding/profiles/{profile_id}/confirm", headers=headers)

    template_resp = await client.get("/api/v1/onboarding/prompt-template", headers=headers)
    text = template_resp.json()["template_text"]
    assert "1000" in text  # Chart of accounts
    assert "US Sub" in text  # Subsidiaries
    assert "transaction_type_field" in text  # SuiteQL naming


@pytest.mark.asyncio
async def test_preview_prompt_template(client: AsyncClient, pro_admin):
    user, headers = pro_admin
    await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Finance", "business_description": "Investment firm"},
        headers=headers,
    )

    resp = await client.get("/api/v1/onboarding/prompt-template/preview", headers=headers)
    assert resp.status_code == 200
    assert "Finance" in resp.json()["template_text"]
    assert "sections" in resp.json()


# ---------------------------------------------------------------------------
# Audit Events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_events_for_create_and_confirm(client: AsyncClient, pro_admin, db: AsyncSession):
    user, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=headers,
    )
    profile_id = create_resp.json()["id"]
    await client.post(f"/api/v1/onboarding/profiles/{profile_id}/confirm", headers=headers)

    from sqlalchemy import select

    from app.models.audit import AuditEvent

    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.tenant_id == user.tenant_id,
            AuditEvent.category == "onboarding",
        )
    )
    events = result.scalars().all()
    actions = [e.action for e in events]
    assert "onboarding.profile_created" in actions
    assert "onboarding.profile_confirmed" in actions


# ---------------------------------------------------------------------------
# Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_profiles(client: AsyncClient, pro_admin, pro_admin_b):
    _, headers_a = pro_admin
    _, headers_b = pro_admin_b

    # Tenant A creates a profile
    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=headers_a,
    )
    profile_id = create_resp.json()["id"]

    # Tenant B cannot access Tenant A's profile
    resp = await client.get(f"/api/v1/onboarding/profiles/{profile_id}", headers=headers_b)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_user_cannot_create_profile(client: AsyncClient, pro_readonly):
    _, headers = pro_readonly
    resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_readonly_user_cannot_confirm_profile(client: AsyncClient, pro_admin, pro_readonly):
    admin_user, admin_headers = pro_admin
    _, readonly_headers = pro_readonly

    create_resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=admin_headers,
    )
    profile_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/onboarding/profiles/{profile_id}/confirm",
        headers=readonly_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Entitlement Gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_plan_cannot_create_profile(client: AsyncClient, admin_user):
    """Free plan tenants should be blocked from onboarding features."""
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/onboarding/profiles",
        json={"industry": "Retail"},
        headers=headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Discover Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_netsuite_metadata(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    resp = await client.post("/api/v1/onboarding/discover", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "chart_of_accounts" in data
    assert "subsidiaries" in data
