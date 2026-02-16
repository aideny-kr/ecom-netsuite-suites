"""Tests for cross-tenant data isolation via RLS."""

from httpx import AsyncClient


class TestCrossTenantIsolation:
    """Tenant A's resources must be invisible to Tenant B."""

    async def test_connections_isolated(self, client: AsyncClient, admin_user, admin_user_b):
        """Connection created by Tenant A is not visible to Tenant B."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        # Tenant A creates a connection
        resp = await client.post(
            "/api/v1/connections",
            json={
                "provider": "shopify",
                "label": "Tenant A Shopify",
                "credentials": {"api_key": "secret"},
            },
            headers=headers_a,
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        # Tenant B lists connections — should NOT see Tenant A's
        resp_b = await client.get("/api/v1/connections", headers=headers_b)
        assert resp_b.status_code == 200
        conn_ids_b = [c["id"] for c in resp_b.json()]
        assert conn_id not in conn_ids_b

    async def test_connections_delete_cross_tenant(self, client: AsyncClient, admin_user, admin_user_b):
        """Tenant B cannot delete Tenant A's connection."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        resp = await client.post(
            "/api/v1/connections",
            json={
                "provider": "netsuite",
                "label": "Tenant A NS",
                "credentials": {"token": "abc"},
            },
            headers=headers_a,
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        # Tenant B tries to delete
        resp_del = await client.delete(f"/api/v1/connections/{conn_id}", headers=headers_b)
        assert resp_del.status_code == 404

    async def test_tables_isolated(self, client: AsyncClient, admin_user, admin_user_b):
        """Table data is scoped per tenant (both see empty tables, not each other's)."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        resp_a = await client.get("/api/v1/tables/orders", headers=headers_a)
        resp_b = await client.get("/api/v1/tables/orders", headers=headers_b)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Both should have 0 items (empty canonical tables)
        assert resp_a.json()["total"] == 0
        assert resp_b.json()["total"] == 0

    async def test_users_isolated(self, client: AsyncClient, admin_user, admin_user_b):
        """Tenant A's user list does not include Tenant B's users."""
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b

        resp_a = await client.get("/api/v1/users", headers=headers_a)
        assert resp_a.status_code == 200
        user_ids_a = {u["id"] for u in resp_a.json()}
        assert str(user_a.id) in user_ids_a
        assert str(user_b.id) not in user_ids_a

        resp_b = await client.get("/api/v1/users", headers=headers_b)
        assert resp_b.status_code == 200
        user_ids_b = {u["id"] for u in resp_b.json()}
        assert str(user_b.id) in user_ids_b
        assert str(user_a.id) not in user_ids_b

    async def test_cross_tenant_user_delete_returns_404(self, client: AsyncClient, db, admin_user, admin_user_b):
        """Tenant B cannot deactivate Tenant A's user (gets 404)."""
        user_a, _ = admin_user
        _, headers_b = admin_user_b

        resp = await client.delete(f"/api/v1/users/{user_a.id}", headers=headers_b)
        assert resp.status_code == 404

    async def test_tenant_config_isolated(self, client: AsyncClient, admin_user, admin_user_b):
        """Each tenant only sees their own config."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        resp_a = await client.get("/api/v1/tenants/me/config", headers=headers_a)
        resp_b = await client.get("/api/v1/tenants/me/config", headers=headers_b)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert resp_a.json()["tenant_id"] != resp_b.json()["tenant_id"]

    async def test_audit_events_isolated(self, client: AsyncClient, admin_user, admin_user_b):
        """Tenant A's audit events are not visible to Tenant B."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        # Tenant A creates a connection (generates audit event)
        await client.post(
            "/api/v1/connections",
            json={
                "provider": "stripe",
                "label": "Stripe A",
                "credentials": {"key": "sk_test"},
            },
            headers=headers_a,
        )

        # Tenant B should not see Tenant A's audit events
        resp_b = await client.get("/api/v1/audit-events", headers=headers_b)
        assert resp_b.status_code == 200
        for event in resp_b.json()["items"]:
            # None of the events should reference connection category from tenant A
            # (Tenant B has no connections, so no connection events)
            pass
        # The key assertion: Tenant B sees 0 connection events
        connection_events = [e for e in resp_b.json()["items"] if e["category"] == "connection"]
        assert len(connection_events) == 0
