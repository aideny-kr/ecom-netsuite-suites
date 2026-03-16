"""Tests for connection health check and update endpoints."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.main import app


@pytest.fixture
def mock_admin():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.email = "admin@example.com"
    ur = MagicMock()
    ur.role_id = uuid.uuid4()
    ur.role = MagicMock()
    ur.role.name = "admin"
    user.user_roles = [ur]
    return user


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    # Make execute return a result whose .all() returns permission codenames
    # This satisfies the require_permission dependency's permission check
    perm_result = MagicMock()
    perm_result.all.return_value = [
        ("connections.view",),
        ("connections.manage",),
    ]
    perm_scalars = MagicMock()
    perm_scalars.all.return_value = []
    exec_result = MagicMock()
    exec_result.all.return_value = perm_result.all.return_value
    exec_result.scalars.return_value = perm_scalars
    db.execute = AsyncMock(return_value=exec_result)
    return db


@pytest.fixture(autouse=True)
def override_deps(mock_admin, mock_db):
    app.dependency_overrides[get_current_user] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()


class TestConnectionHealth:
    @pytest.mark.asyncio
    async def test_health_returns_items(self, mock_db):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/connections/health",
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        data = response.json()
        assert "connections" in data
        assert "mcp_connectors" in data

    @pytest.mark.asyncio
    async def test_health_requires_auth(self):
        app.dependency_overrides.clear()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/connections/health")
        assert response.status_code in (401, 403)


class TestUpdateClientId:
    @pytest.mark.asyncio
    async def test_update_client_id(self, mock_db):
        with patch("app.api.v1.connections.connection_service") as mock_svc:
            conn = MagicMock()
            conn.id = uuid.uuid4()
            conn.encrypted_credentials = "encrypted_blob"
            mock_svc.get_connection = AsyncMock(return_value=conn)

            with (
                patch(
                    "app.api.v1.connections.decrypt_credentials",
                    return_value={"client_id": "old"},
                ),
                patch(
                    "app.api.v1.connections.encrypt_credentials",
                    return_value="new_encrypted",
                ),
                patch("app.api.v1.connections.audit_service") as mock_audit,
            ):
                mock_audit.log_event = AsyncMock()
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.patch(
                        f"/api/v1/connections/{uuid.uuid4()}/client-id",
                        json={"client_id": "new-client-id"},
                        headers={"Authorization": "Bearer test"},
                    )
                assert response.status_code == 200
                assert response.json()["client_id"] == "new-client-id"

    @pytest.mark.asyncio
    async def test_update_client_id_not_found(self, mock_db):
        with patch("app.api.v1.connections.connection_service") as mock_svc:
            mock_svc.get_connection = AsyncMock(return_value=None)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.patch(
                    f"/api/v1/connections/{uuid.uuid4()}/client-id",
                    json={"client_id": "new-client-id"},
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 404


class TestUpdateRestletUrl:
    @pytest.mark.asyncio
    async def test_update_restlet_url(self, mock_db):
        with patch("app.api.v1.connections.connection_service") as mock_svc:
            conn = MagicMock()
            conn.id = uuid.uuid4()
            conn.metadata_json = {}
            mock_svc.get_connection = AsyncMock(return_value=conn)

            with patch("app.api.v1.connections.audit_service") as mock_audit:
                mock_audit.log_event = AsyncMock()
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.patch(
                        f"/api/v1/connections/{uuid.uuid4()}/restlet-url",
                        json={"restlet_url": "https://example.com/restlet"},
                        headers={"Authorization": "Bearer test"},
                    )
                assert response.status_code == 200
                assert response.json()["restlet_url"] == "https://example.com/restlet"

    @pytest.mark.asyncio
    async def test_update_restlet_url_not_found(self, mock_db):
        with patch("app.api.v1.connections.connection_service") as mock_svc:
            mock_svc.get_connection = AsyncMock(return_value=None)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.patch(
                    f"/api/v1/connections/{uuid.uuid4()}/restlet-url",
                    json={"restlet_url": "https://example.com/restlet"},
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 404
