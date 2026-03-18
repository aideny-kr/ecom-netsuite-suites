"""TDD tests: token refresh MUST use stored per-connection client_id.

The root cause of all token expiry issues: get_valid_token() and the
proactive refresh task used settings.NETSUITE_OAUTH_CLIENT_ID (global)
instead of the stored per-connection client_id. When global != stored,
NetSuite returns invalid_grant on every refresh attempt.
"""

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test 1: get_valid_token() uses STORED client_id, not global
# ---------------------------------------------------------------------------

class TestGetValidTokenClientId:
    @pytest.mark.asyncio
    async def test_refresh_uses_stored_client_id_not_global(self):
        """When refreshing an expired token, get_valid_token() must use the
        client_id stored in the connection's encrypted_credentials, NOT the
        global settings.NETSUITE_OAUTH_CLIENT_ID."""
        from app.services.netsuite_oauth_service import get_valid_token

        stored_client_id = "stored_per_connection_client_id_abc123"
        global_client_id = "wrong_global_client_id_xyz789"

        credentials = {
            "auth_type": "oauth2",
            "access_token": "expired_token",
            "refresh_token": "valid_refresh_token",
            "expires_at": time.time() - 100,  # Already expired
            "account_id": "6738075",
            "client_id": stored_client_id,
        }

        connection = MagicMock()
        connection.id = uuid.uuid4()
        connection.encrypted_credentials = "encrypted_blob"

        db = AsyncMock()
        db.refresh = AsyncMock()
        db.commit = AsyncMock()

        new_token_data = {
            "access_token": "fresh_new_token",
            "refresh_token": "fresh_refresh_token",
            "expires_in": 3600,
        }

        with (
            patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials),
            patch("app.services.netsuite_oauth_service.encrypt_credentials", return_value="new_encrypted"),
            patch("app.services.netsuite_oauth_service.refresh_tokens_with_client", new_callable=AsyncMock, return_value=new_token_data) as mock_refresh,
            patch("app.services.netsuite_oauth_service.settings") as mock_settings,
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
        ):
            mock_settings.NETSUITE_OAUTH_CLIENT_ID = global_client_id

            result = await get_valid_token(db, connection)

        # MUST use stored client_id, NOT global
        mock_refresh.assert_awaited_once()
        actual_client_id = mock_refresh.call_args[0][2]  # 3rd positional arg
        assert actual_client_id == stored_client_id, (
            f"Expected stored client_id '{stored_client_id}', "
            f"got '{actual_client_id}'. "
            f"get_valid_token() must use per-connection client_id, not global."
        )
        assert result == "fresh_new_token"

    @pytest.mark.asyncio
    async def test_refresh_works_when_global_client_id_is_empty(self):
        """Even when NETSUITE_OAUTH_CLIENT_ID is empty, refresh should work
        using the stored per-connection client_id."""
        from app.services.netsuite_oauth_service import get_valid_token

        stored_client_id = "stored_client_id_for_this_connection"

        credentials = {
            "auth_type": "oauth2",
            "access_token": "expired_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
            "account_id": "6738075",
            "client_id": stored_client_id,
        }

        connection = MagicMock()
        connection.id = uuid.uuid4()
        connection.encrypted_credentials = "encrypted_blob"

        db = AsyncMock()
        db.refresh = AsyncMock()
        db.commit = AsyncMock()

        new_token_data = {
            "access_token": "new_token",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }

        with (
            patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials),
            patch("app.services.netsuite_oauth_service.encrypt_credentials", return_value="enc"),
            patch("app.services.netsuite_oauth_service.refresh_tokens_with_client", new_callable=AsyncMock, return_value=new_token_data) as mock_refresh,
            patch("app.services.netsuite_oauth_service.settings") as mock_settings,
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
        ):
            mock_settings.NETSUITE_OAUTH_CLIENT_ID = ""  # Empty global

            result = await get_valid_token(db, connection)

        mock_refresh.assert_awaited_once()
        actual_client_id = mock_refresh.call_args[0][2]
        assert actual_client_id == stored_client_id

    @pytest.mark.asyncio
    async def test_returns_none_when_no_client_id_stored(self):
        """If connection has no client_id stored and global is empty,
        refresh should fail gracefully (return None)."""
        from app.services.netsuite_oauth_service import get_valid_token

        credentials = {
            "auth_type": "oauth2",
            "access_token": "expired_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
            "account_id": "6738075",
            # No client_id stored
        }

        connection = MagicMock()
        connection.id = uuid.uuid4()
        connection.encrypted_credentials = "encrypted_blob"

        db = AsyncMock()
        db.refresh = AsyncMock()

        with (
            patch("app.services.netsuite_oauth_service.decrypt_credentials", return_value=credentials),
            patch("app.services.netsuite_oauth_service.settings") as mock_settings,
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
        ):
            mock_settings.NETSUITE_OAUTH_CLIENT_ID = ""

            result = await get_valid_token(db, connection)

        assert result is None


# ---------------------------------------------------------------------------
# Test 2: Proactive refresh task uses STORED client_id for REST connections
# ---------------------------------------------------------------------------

class TestProactiveRefreshClientId:
    def test_rest_connection_uses_stored_client_id(self):
        """Proactive refresh for REST connections must use the stored
        per-connection client_id, not settings.NETSUITE_OAUTH_CLIENT_ID."""
        from app.workers.tasks.proactive_token_refresh import _refresh_single
        from datetime import datetime, timezone

        stored_client_id = "stored_rest_client_id_abc"

        record = MagicMock()
        record.id = uuid.uuid4()
        record.tenant_id = uuid.uuid4()
        record.status = "active"
        record.error_reason = None
        record.encrypted_credentials = "blob"

        creds = {
            "auth_type": "oauth2",
            "access_token": "old",
            "refresh_token": "refresh_tok",
            "expires_at": time.time() + 300,  # 5 min left — within buffer
            "account_id": "6738075",
            "client_id": stored_client_id,
        }

        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        mock_settings = MagicMock()
        mock_settings.NETSUITE_OAUTH_CLIENT_ID = "wrong_global_id"

        token_data = {"access_token": "new", "refresh_token": "new_rt", "expires_in": 3600}

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.encryption.encrypt_credentials", return_value="enc"),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh", return_value=token_data) as mock_refresh,
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), mock_settings)

        mock_refresh.assert_called_once()
        actual_client_id = mock_refresh.call_args[0][2]
        assert actual_client_id == stored_client_id, (
            f"Expected stored '{stored_client_id}', got '{actual_client_id}'. "
            f"Proactive task must use per-connection client_id for REST."
        )
        assert stats["refreshed"] == 1

    def test_mcp_connection_uses_stored_client_id(self):
        """MCP connectors must also use stored client_id (already correct,
        but verify it stays correct)."""
        from app.workers.tasks.proactive_token_refresh import _refresh_single
        from datetime import datetime, timezone

        stored_client_id = "stored_mcp_client_id_xyz"

        record = MagicMock()
        record.id = uuid.uuid4()
        record.tenant_id = uuid.uuid4()
        record.status = "active"
        record.error_reason = None
        record.encrypted_credentials = "blob"

        creds = {
            "auth_type": "oauth2",
            "access_token": "old",
            "refresh_token": "refresh_tok",
            "expires_at": time.time() + 300,
            "account_id": "6738075",
            "client_id": stored_client_id,
        }

        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        mock_settings = MagicMock()
        mock_settings.NETSUITE_OAUTH_CLIENT_ID = "wrong_global_id"

        token_data = {"access_token": "new", "expires_in": 3600}

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.encryption.encrypt_credentials", return_value="enc"),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh", return_value=token_data) as mock_refresh,
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh:mcp", stats, datetime.now(timezone.utc), mock_settings)

        actual_client_id = mock_refresh.call_args[0][2]
        assert actual_client_id == stored_client_id


# ---------------------------------------------------------------------------
# Test 3: Proactive task logs actual error body on refresh failure
# ---------------------------------------------------------------------------

class TestProactiveRefreshErrorLogging:
    def test_logs_error_details_on_refresh_failure(self):
        """When refresh fails, the task must log the actual error message
        (e.g., 'invalid_grant'), not just 'refresh_failed'."""
        from app.workers.tasks.proactive_token_refresh import _refresh_single
        from datetime import datetime, timezone

        record = MagicMock()
        record.id = uuid.uuid4()
        record.tenant_id = uuid.uuid4()
        record.status = "active"
        record.error_reason = None
        record.encrypted_credentials = "blob"

        creds = {
            "auth_type": "oauth2",
            "access_token": "old",
            "refresh_token": "dead_refresh",
            "expires_at": time.time() + 300,
            "account_id": "6738075",
            "client_id": "some_client_id",
        }

        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        mock_settings = MagicMock()
        mock_settings.NETSUITE_OAUTH_CLIENT_ID = ""

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh",
                  side_effect=Exception('{"error":"invalid_grant"}')),
            patch("app.workers.tasks.proactive_token_refresh.logger") as mock_logger,
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), mock_settings)

        assert stats["errors"] == 1
        # Verify the error message includes the actual error detail
        mock_logger.warning.assert_called()
        log_call = mock_logger.warning.call_args
        assert "invalid_grant" in str(log_call)
