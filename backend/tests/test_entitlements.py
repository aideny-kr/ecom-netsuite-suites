"""Tests for plan entitlement enforcement."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.services import entitlement_service
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


class TestConnectionEntitlements:
    """Trial plan limits connections to 2."""

    async def test_trial_can_create_up_to_limit(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Trial Ent", plan="trial")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Create first connection — should succeed
        resp1 = await client.post("/api/v1/connections", json={
            "provider": "shopify",
            "label": "First",
            "credentials": {"key": "val"},
        }, headers=headers)
        assert resp1.status_code == 201

        # Create second connection — should succeed (limit is 2)
        resp2 = await client.post("/api/v1/connections", json={
            "provider": "stripe",
            "label": "Second",
            "credentials": {"key": "val"},
        }, headers=headers)
        assert resp2.status_code == 201

    async def test_trial_blocked_beyond_limit(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Trial Block", plan="trial")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Create 2 connections (the trial limit)
        for i in range(2):
            resp = await client.post("/api/v1/connections", json={
                "provider": "shopify",
                "label": f"Conn {i}",
                "credentials": {"key": f"val{i}"},
            }, headers=headers)
            assert resp.status_code == 201

        # Third connection should be blocked
        resp3 = await client.post("/api/v1/connections", json={
            "provider": "netsuite",
            "label": "Third",
            "credentials": {"key": "val"},
        }, headers=headers)
        assert resp3.status_code == 403
        assert "limit" in resp3.json()["detail"].lower() or "plan" in resp3.json()["detail"].lower()

    async def test_pro_has_higher_limit(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Pro Ent", plan="pro")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        # Pro plan allows up to 50 — create 3 and verify all succeed
        for i in range(3):
            resp = await client.post("/api/v1/connections", json={
                "provider": "shopify",
                "label": f"Pro Conn {i}",
                "credentials": {"key": f"val{i}"},
            }, headers=headers)
            assert resp.status_code == 201


class TestEntitlementServiceDirect:
    """Unit tests for entitlement_service.check_entitlement."""

    async def test_trial_connections_allowed(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Ent Direct 1", plan="trial")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "connections")
        assert result is True  # No connections yet, so allowed

    async def test_mcp_tools_denied_on_trial(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Ent MCP", plan="trial")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "mcp_tools")
        assert result is False

    async def test_mcp_tools_allowed_on_pro(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Ent MCP Pro", plan="pro")
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "mcp_tools")
        assert result is True

    async def test_get_plan_limits(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Limits", plan="trial")
        await db.commit()
        limits = await entitlement_service.get_plan_limits(db, tenant.id)
        assert limits["max_connections"] == 2
        assert limits["mcp_tools"] is False

    async def test_inactive_tenant_denied(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Inactive", plan="pro")
        tenant.is_active = False
        await db.commit()
        result = await entitlement_service.check_entitlement(db, tenant.id, "connections")
        assert result is False
