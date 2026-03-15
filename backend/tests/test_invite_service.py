"""Tests for invite_service."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.invite_service import (
    ROLE_DISPLAY_NAMES,
    VALID_INVITE_ROLES,
)


class TestConstants:
    def test_valid_roles(self):
        assert "admin" in VALID_INVITE_ROLES
        assert "finance" in VALID_INVITE_ROLES
        assert "ops" in VALID_INVITE_ROLES
        assert "readonly" not in VALID_INVITE_ROLES

    def test_role_display_names(self):
        assert ROLE_DISPLAY_NAMES["admin"] == "Admin"
        assert ROLE_DISPLAY_NAMES["finance"] == "Finance"
        assert ROLE_DISPLAY_NAMES["ops"] == "Operations"


class TestCreateInvite:
    @pytest.mark.asyncio
    async def test_invalid_role_raises(self):
        from app.services.invite_service import create_invite

        mock_db = AsyncMock()
        with pytest.raises(ValueError, match="Invalid role"):
            await create_invite(
                db=mock_db,
                tenant_id=uuid.uuid4(),
                email="test@example.com",
                role_name="nonexistent",
                invited_by=uuid.uuid4(),
                inviter_name="Admin",
                tenant_brand_name="Acme",
            )


class TestAcceptInvite:
    @pytest.mark.asyncio
    async def test_expired_invite_raises(self):
        from app.services.invite_service import accept_invite
        from app.models.invite import Invite

        mock_db = AsyncMock()
        expired_invite = MagicMock(spec=Invite)
        expired_invite.status = "pending"
        expired_invite.expires_at = datetime.now(timezone.utc) - timedelta(days=1)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expired_invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="expired"):
            await accept_invite(
                db=mock_db,
                token="test-token",
                full_name="Test User",
                password="TestPass1!",
            )

    @pytest.mark.asyncio
    async def test_already_accepted_raises(self):
        from app.services.invite_service import accept_invite
        from app.models.invite import Invite

        mock_db = AsyncMock()
        accepted_invite = MagicMock(spec=Invite)
        accepted_invite.status = "accepted"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = accepted_invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="already been accepted"):
            await accept_invite(
                db=mock_db,
                token="test-token",
                full_name="Test User",
                password="TestPass1!",
            )

    @pytest.mark.asyncio
    async def test_no_auth_method_raises(self):
        from app.services.invite_service import accept_invite
        from app.models.invite import Invite

        mock_db = AsyncMock()
        invite = MagicMock(spec=Invite)
        invite.status = "pending"
        invite.expires_at = datetime.now(timezone.utc) + timedelta(days=5)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="password or Google"):
            await accept_invite(
                db=mock_db,
                token="test-token",
                full_name="Test User",
            )


class TestRevokeInvite:
    @pytest.mark.asyncio
    async def test_revoke_sets_status(self):
        from app.services.invite_service import revoke_invite
        from app.models.invite import Invite

        mock_db = AsyncMock()
        invite = MagicMock(spec=Invite)
        invite.status = "pending"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        await revoke_invite(mock_db, uuid.uuid4(), uuid.uuid4())
        assert invite.status == "revoked"

    @pytest.mark.asyncio
    async def test_revoke_non_pending_raises(self):
        from app.services.invite_service import revoke_invite
        from app.models.invite import Invite

        mock_db = AsyncMock()
        invite = MagicMock(spec=Invite)
        invite.status = "accepted"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="pending"):
            await revoke_invite(mock_db, uuid.uuid4(), uuid.uuid4())
