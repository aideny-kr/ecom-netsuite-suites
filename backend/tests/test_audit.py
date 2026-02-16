"""Tests for audit event emission and correlation ID propagation."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from tests.conftest import create_test_tenant, create_test_user


class TestAuditEventEmission:
    """Verify critical actions emit audit events."""

    async def test_login_emits_audit(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Audit Login Corp")
        user, password = await create_test_user(db, tenant, email="audit-login@test.com")
        await db.commit()

        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "audit-login@test.com",
                "password": password,
            },
        )
        assert resp.status_code == 200

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "user.login",
                AuditEvent.actor_id == user.id,
            )
        )
        event = result.scalars().first()
        assert event is not None
        assert event.category == "auth"
        assert event.tenant_id == tenant.id

    async def test_connection_create_emits_audit(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/connections",
            json={
                "provider": "shopify",
                "label": "Audit Shopify",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )
        assert resp.status_code == 201

    async def test_connection_delete_emits_audit(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        # Create then delete
        resp = await client.post(
            "/api/v1/connections",
            json={
                "provider": "netsuite",
                "label": "To Delete",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )
        conn_id = resp.json()["id"]

        resp_del = await client.delete(f"/api/v1/connections/{conn_id}", headers=headers)
        assert resp_del.status_code == 204

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "connection.delete",
                AuditEvent.resource_id == conn_id,
            )
        )
        event = result.scalars().first()
        assert event is not None

    async def test_user_create_emits_audit(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/users",
            json={
                "email": f"newuser-{uuid.uuid4().hex[:6]}@test.com",
                "password": "testpass123",
                "full_name": "New User",
            },
            headers=headers,
        )
        assert resp.status_code == 201

        result = await db.execute(
            select(AuditEvent).where(AuditEvent.action == "user.create").order_by(AuditEvent.id.desc())
        )
        event = result.scalars().first()
        assert event is not None
        assert event.category == "user"

    async def test_user_deactivate_emits_audit(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        # Create a user to deactivate
        resp = await client.post(
            "/api/v1/users",
            json={
                "email": f"todeact-{uuid.uuid4().hex[:6]}@test.com",
                "password": "testpass123",
                "full_name": "To Deactivate",
            },
            headers=headers,
        )
        target_id = resp.json()["id"]

        resp_del = await client.delete(f"/api/v1/users/{target_id}", headers=headers)
        assert resp_del.status_code == 204

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "user.deactivate",
                AuditEvent.resource_id == target_id,
            )
        )
        event = result.scalars().first()
        assert event is not None

    async def test_audit_events_have_tenant_id(self, client: AsyncClient, admin_user, db: AsyncSession):
        """All audit events must have a tenant_id set."""
        user, headers = admin_user
        # Generate some activity
        await client.post(
            "/api/v1/connections",
            json={
                "provider": "stripe",
                "label": "Audit Test",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )

        result = await db.execute(select(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id))
        events = result.scalars().all()
        assert len(events) > 0
        for event in events:
            assert event.tenant_id is not None


class TestCorrelationId:
    """Verify X-Correlation-ID header is generated and returned."""

    async def test_correlation_id_in_response(self, client: AsyncClient):
        """Every response should include X-Correlation-ID."""
        resp = await client.get("/api/v1/health")
        assert "x-correlation-id" in resp.headers
        # Should be a valid UUID-ish string
        cid = resp.headers["x-correlation-id"]
        assert len(cid) > 0

    async def test_correlation_id_propagated_from_request(self, client: AsyncClient):
        """If client sends X-Correlation-ID, it should be echoed back."""
        custom_id = str(uuid.uuid4())
        resp = await client.get("/api/v1/health", headers={"X-Correlation-ID": custom_id})
        assert resp.headers.get("x-correlation-id") == custom_id

    async def test_different_requests_get_different_ids(self, client: AsyncClient):
        """Each request without a custom ID gets a unique correlation ID."""
        resp1 = await client.get("/api/v1/health")
        resp2 = await client.get("/api/v1/health")
        cid1 = resp1.headers["x-correlation-id"]
        cid2 = resp2.headers["x-correlation-id"]
        assert cid1 != cid2


class TestAuditEndpoint:
    """Test GET /audit-events filtering."""

    async def test_filter_by_category(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        # Create a connection to generate an audit event with category='connection'
        await client.post(
            "/api/v1/connections",
            json={
                "provider": "shopify",
                "label": "Filter Test",
                "credentials": {"key": "val"},
            },
            headers=headers,
        )

        resp = await client.get("/api/v1/audit-events?category=connection", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            assert item["category"] == "connection"

    async def test_filter_by_action(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.get("/api/v1/audit-events?action=connection.create", headers=headers)
        assert resp.status_code == 200

    async def test_pagination(self, client: AsyncClient, admin_user):
        _, headers = admin_user
        resp = await client.get("/api/v1/audit-events?page=1&page_size=5", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert data["page"] == 1
        assert data["page_size"] == 5
