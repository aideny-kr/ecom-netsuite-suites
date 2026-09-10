"""Tests for plan entitlement enforcement."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import entitlement_service
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


class TestConnectionEntitlements:
    """Connection counts are unlimited on every plan."""

    async def test_free_can_create_connections(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Trial Ent", plan="free")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Create first connection — should succeed
        resp1 = await client.post(
            "/api/v1/connections",
            json={
                "provider": "shopify",
                "label": "First",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )
        assert resp1.status_code == 201

        # Create second connection — should succeed
        resp2 = await client.post(
            "/api/v1/connections",
            json={
                "provider": "stripe",
                "label": "Second",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )
        assert resp2.status_code == 201

    async def test_free_allows_connections_beyond_former_limit(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Trial Unlimited", plan="free")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Create the two connections that previously exhausted the free plan
        for i in range(2):
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider": "shopify",
                    "label": f"Conn {i}",
                    "credentials": {"key": f"val{i}"},
                },
                headers=headers,
            )
            assert resp.status_code == 201

        # A third connection must succeed without an upgrade or deletion
        resp3 = await client.post(
            "/api/v1/connections",
            json={
                "provider": "stripe",
                "label": "Third",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )
        assert resp3.status_code == 201

    async def test_free_netsuite_always_allowed(self, client: AsyncClient, db: AsyncSession):
        """NetSuite is the core product — always allowed even on free plan, doesn't count against limit."""
        tenant = await create_test_tenant(db, name="Trial NS", plan="free")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Create NetSuite connection first
        resp1 = await client.post(
            "/api/v1/connections",
            json={
                "provider": "netsuite",
                "label": "NetSuite Prod",
                "credentials": {"account_id": "123", "token": "tok"},
            },
            headers=headers,
        )
        assert resp1.status_code == 201

        # Should still be able to create 2 non-NetSuite connections
        for i, provider in enumerate(["shopify", "stripe"]):
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider": provider,
                    "label": f"Conn {i}",
                    "credentials": {"key": f"val{i}"},
                },
                headers=headers,
            )
            assert resp.status_code == 201

    async def test_pro_allows_connections(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Pro Ent", plan="pro")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Pro connections remain available
        for i in range(3):
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider": "shopify",
                    "label": f"Pro Conn {i}",
                    "credentials": {"key": f"val{i}"},
                },
                headers=headers,
            )
            assert resp.status_code == 201


class TestEntitlementServiceDirect:
    """Unit tests for entitlement_service.check_entitlement."""

    async def test_free_connections_allowed(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Ent Direct 1", plan="free")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "connections")
        assert result is True  # No connections yet, so allowed

    async def test_mcp_tools_denied_on_trial(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Ent MCP", plan="free")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "mcp_tools")
        assert result is False

    async def test_mcp_tools_allowed_on_pro(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Ent MCP Pro", plan="pro")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "mcp_tools")
        assert result is True

    async def test_get_plan_limits(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Limits", plan="free")
        await db.commit()
        limits = await entitlement_service.get_plan_limits(db, tenant.id)
        assert limits["max_connections"] == -1
        assert limits["mcp_tools"] is False

    async def test_inactive_tenant_denied(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Inactive", plan="pro")
        tenant.is_active = False
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "connections")
        assert result is False

    async def test_chat_allowed_on_all_plans(self, db: AsyncSession):
        for plan in ("free", "pro", "max"):
            tenant = await create_test_tenant(db, name=f"Chat {plan}", plan=plan)
            await db.commit()
            result = await entitlement_service.check_entitlement(db, tenant.id, "chat")
            assert result is True, f"Chat should be allowed on {plan}"

    async def test_byok_ai_denied_on_free(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="BYOK Free", plan="free")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "byok_ai")
        assert result is False

    async def test_byok_ai_allowed_on_pro(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="BYOK Pro", plan="pro")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "byok_ai")
        assert result is True

    async def test_byok_ai_allowed_on_max(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="BYOK Max", plan="max")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "byok_ai")
        assert result is True

    async def test_max_plan_unlimited_connections(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Max Conn", plan="max")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "connections")
        assert result is True

    async def test_get_usage_summary(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Usage", plan="free")
        await db.commit()
        usage = await entitlement_service.get_usage_summary(db, tenant.id)
        assert usage["connections"] == 0
        assert usage["schedules"] == 0


class TestPlanInfoAPI:
    """Tests for GET /api/v1/tenants/me/plan."""

    async def test_plan_info_returns_correct_data(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Plan Info", plan="free")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        resp = await client.get("/api/v1/tenants/me/plan", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan"] == "free"
        assert data["limits"]["max_connections"] == -1
        assert data["limits"]["max_schedules"] == 5
        assert data["limits"]["mcp_tools"] is False
        assert data["limits"]["chat"] is True
        assert data["limits"]["byok_ai"] is False
        assert data["usage"]["connections"] == 0
        assert data["usage"]["schedules"] == 0

    async def test_plan_info_pro(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Plan Pro", plan="pro")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        resp = await client.get("/api/v1/tenants/me/plan", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan"] == "pro"
        assert data["limits"]["max_connections"] == -1
        assert data["limits"]["mcp_tools"] is True
        assert data["limits"]["byok_ai"] is True

    async def test_plan_info_max(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Plan Max", plan="max")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        resp = await client.get("/api/v1/tenants/me/plan", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan"] == "max"
        assert data["limits"]["max_connections"] == -1
        assert data["limits"]["max_exports_per_day"] == -1
