"""Tests for invite API endpoints."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1 import invites as invites_module
from app.core.dependencies import get_current_user
from app.core.database import get_db
from app.main import app
from app.models.invite import Invite


@pytest.fixture
def mock_admin():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.email = "admin@example.com"
    user.full_name = "Admin User"
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
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def override_deps(mock_admin, mock_db):
    # Override auth + permission + db dependencies
    app.dependency_overrides[get_current_user] = lambda: mock_admin
    app.dependency_overrides[invites_module._users_manage] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()


class TestCreateInvite:
    @pytest.mark.asyncio
    async def test_returns_201(self, mock_admin, mock_db):
        with patch("app.api.v1.invites.invite_service") as mock_svc:
            invite = MagicMock(spec=Invite)
            invite.id = uuid.uuid4()
            invite.email = "new@example.com"
            invite.role_name = "finance"
            invite.status = "pending"
            invite.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
            invite.created_at = datetime.now(timezone.utc)
            mock_svc.create_invite = AsyncMock(return_value=invite)

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/invites",
                    json={"email": "new@example.com", "role_name": "finance"},
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 201
            data = response.json()
            assert data["email"] == "new@example.com"
            assert data["role_display_name"] == "User"

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app.dependency_overrides.clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/invites",
                json={"email": "new@example.com", "role_name": "finance"},
            )
        assert response.status_code in (401, 403)


class TestListInvites:
    @pytest.mark.asyncio
    async def test_returns_list(self):
        with patch("app.api.v1.invites.invite_service") as mock_svc:
            mock_svc.list_invites = AsyncMock(return_value=[])
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(
                    "/api/v1/invites",
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 200
            assert response.json() == []


class TestAcceptInvite:
    @pytest.mark.asyncio
    async def test_public_endpoint(self, mock_db):
        app.dependency_overrides.clear()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("app.api.v1.invites.invite_service") as mock_svc:
            user = MagicMock()
            user.id = uuid.uuid4()
            user.tenant_id = uuid.uuid4()
            user.email = "new@example.com"
            user.full_name = "New User"
            tokens = {"access_token": "test-access", "refresh_token": "test-refresh"}
            mock_svc.accept_invite = AsyncMock(return_value=(user, tokens))

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/invites/accept/test-token-123",
                    json={"full_name": "New User", "password": "TestPass1!"},
                )
            assert response.status_code == 200
            assert "access_token" in response.json()


class TestGetInviteInfo:
    @pytest.mark.asyncio
    async def test_returns_invite_details(self, mock_db):
        app.dependency_overrides.clear()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("app.api.v1.invites.invite_service") as mock_svc:
            invite = MagicMock(spec=Invite)
            invite.email = "new@example.com"
            invite.role_name = "finance"
            invite.status = "pending"
            invite.expires_at = datetime.now(timezone.utc) + timedelta(days=5)
            invite.tenant_id = uuid.uuid4()
            mock_svc.get_invite_by_token = AsyncMock(return_value=invite)

            with patch("app.api.v1.invites._get_tenant_name", new_callable=AsyncMock, return_value="Acme Corp"):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.get("/api/v1/invites/accept/test-token")
                assert response.status_code == 200
                data = response.json()
                assert data["email"] == "new@example.com"
                assert data["role_display_name"] == "User"
