"""Tests for RBAC permission enforcement across all protected endpoints."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.user import User
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


class TestAdminAccess:
    """Admin should have access to all endpoints."""

    async def test_admin_list_connections(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/connections", headers=headers)
        assert resp.status_code == 200

    async def test_admin_create_connection(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "shopify",
            "label": "Test Shopify",
            "credentials": {"api_key": "test123"},
        }, headers=headers)
        assert resp.status_code == 201

    async def test_admin_list_users(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 200

    async def test_admin_create_user(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.post("/api/v1/users", json={
            "email": f"new-{uuid.uuid4().hex[:6]}@test.com",
            "password": "newuserpass123",
            "full_name": "New User",
        }, headers=headers)
        assert resp.status_code == 201

    async def test_admin_get_tenant(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/tenants/me", headers=headers)
        assert resp.status_code == 200

    async def test_admin_get_tenant_config(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/tenants/me/config", headers=headers)
        assert resp.status_code == 200

    async def test_admin_view_tables(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/tables/orders", headers=headers)
        assert resp.status_code == 200

    async def test_admin_view_audit(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/audit-events", headers=headers)
        assert resp.status_code == 200


class TestReadonlyAccess:
    """Readonly users can only read tables — everything else should 403."""

    async def test_readonly_can_view_tables(self, client: AsyncClient, readonly_user):
        _, headers = readonly_user
        resp = await client.get("/api/v1/tables/orders", headers=headers)
        assert resp.status_code == 200

    async def test_readonly_cannot_create_connection(self, client: AsyncClient, readonly_user):
        _, headers = readonly_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "shopify",
            "label": "Test",
            "credentials": {"key": "val"},
        }, headers=headers)
        assert resp.status_code == 403

    async def test_readonly_can_list_connections(self, client: AsyncClient, readonly_user):
        """Readonly has connections.view so can list but not create."""
        _, headers = readonly_user
        resp = await client.get("/api/v1/connections", headers=headers)
        assert resp.status_code == 200

    async def test_readonly_cannot_manage_users(self, client: AsyncClient, readonly_user):
        _, headers = readonly_user
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 403

    async def test_readonly_cannot_create_user(self, client: AsyncClient, readonly_user):
        _, headers = readonly_user
        resp = await client.post("/api/v1/users", json={
            "email": "new@test.com",
            "password": "password123",
            "full_name": "New User",
        }, headers=headers)
        assert resp.status_code == 403

    async def test_readonly_cannot_update_tenant(self, client: AsyncClient, readonly_user):
        _, headers = readonly_user
        resp = await client.patch("/api/v1/tenants/me", json={"name": "Hacked"}, headers=headers)
        assert resp.status_code == 403

    async def test_readonly_cannot_update_config(self, client: AsyncClient, readonly_user):
        _, headers = readonly_user
        resp = await client.patch("/api/v1/tenants/me/config", json={
            "posting_mode": "detail",
        }, headers=headers)
        assert resp.status_code == 403

    async def test_readonly_can_view_audit(self, client: AsyncClient, readonly_user):
        """Readonly has audit.view permission."""
        _, headers = readonly_user
        resp = await client.get("/api/v1/audit-events", headers=headers)
        assert resp.status_code == 200


class TestFinanceAccess:
    """Finance users can view connections/tables/audit but cannot write connections or manage config/users."""

    async def test_finance_can_view_connections(self, client: AsyncClient, finance_user):
        _, headers = finance_user
        resp = await client.get("/api/v1/connections", headers=headers)
        assert resp.status_code == 200

    async def test_finance_can_view_tables(self, client: AsyncClient, finance_user):
        _, headers = finance_user
        resp = await client.get("/api/v1/tables/orders", headers=headers)
        assert resp.status_code == 200

    async def test_finance_can_view_audit(self, client: AsyncClient, finance_user):
        _, headers = finance_user
        resp = await client.get("/api/v1/audit-events", headers=headers)
        assert resp.status_code == 200

    async def test_finance_cannot_create_connection(self, client: AsyncClient, finance_user):
        _, headers = finance_user
        resp = await client.post("/api/v1/connections", json={
            "provider": "shopify",
            "label": "Test",
            "credentials": {"key": "val"},
        }, headers=headers)
        assert resp.status_code == 403

    async def test_finance_cannot_manage_users(self, client: AsyncClient, finance_user):
        _, headers = finance_user
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 403

    async def test_finance_cannot_update_config(self, client: AsyncClient, finance_user):
        _, headers = finance_user
        resp = await client.patch("/api/v1/tenants/me/config", json={
            "posting_mode": "detail",
        }, headers=headers)
        assert resp.status_code == 403


class TestUnauthenticatedAccess:
    """Unauthenticated requests should be rejected on protected endpoints."""

    async def test_no_auth_connections(self, client: AsyncClient):
        resp = await client.get("/api/v1/connections")
        assert resp.status_code in (401, 403)

    async def test_no_auth_tables(self, client: AsyncClient):
        resp = await client.get("/api/v1/tables/orders")
        assert resp.status_code in (401, 403)

    async def test_no_auth_users(self, client: AsyncClient):
        resp = await client.get("/api/v1/users")
        assert resp.status_code in (401, 403)

    async def test_no_auth_audit(self, client: AsyncClient):
        resp = await client.get("/api/v1/audit-events")
        assert resp.status_code in (401, 403)

    async def test_health_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
