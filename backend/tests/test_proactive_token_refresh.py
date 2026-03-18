"""Tests for proactive token refresh task."""

import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.workers.tasks.proactive_token_refresh import REFRESH_BUFFER_SECONDS, _refresh_single


def _make_record(expires_in_seconds: int, auth_type: str = "oauth2"):
    """Create a mock connection/connector record."""
    record = MagicMock()
    record.id = uuid.uuid4()
    record.tenant_id = uuid.uuid4()
    record.status = "active"
    record.error_reason = None
    record.last_health_check_at = None

    creds = {
        "auth_type": auth_type,
        "access_token": "old_token",
        "refresh_token": "refresh_tok",
        "expires_at": time.time() + expires_in_seconds,
        "account_id": "1234567",
        "client_id": "test_client_id",
    }

    record.encrypted_credentials = "encrypted_blob"
    return record, creds


class TestProactiveRefresh:
    def test_skips_token_not_expiring_soon(self):
        """Tokens with >10 min remaining should be skipped."""
        record, creds = _make_record(expires_in_seconds=900)  # 15 min left
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()
        settings.NETSUITE_OAUTH_CLIENT_ID = "global_client"

        with patch("app.core.encryption.decrypt_credentials", return_value=creds):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), settings)

        assert stats["refreshed"] == 0

    def test_refreshes_token_expiring_soon(self):
        """Tokens expiring within 10 min should be refreshed."""
        record, creds = _make_record(expires_in_seconds=300)  # 5 min left
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()
        settings.NETSUITE_OAUTH_CLIENT_ID = "global_client"

        token_data = {
            "access_token": "new_token",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.encryption.encrypt_credentials", return_value="new_encrypted"),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh", return_value=token_data),
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), settings)

        assert stats["refreshed"] == 1
        assert record.status == "active"
        assert record.error_reason is None

    def test_skips_when_lock_held(self):
        """Should skip refresh if another process holds the lock."""
        record, creds = _make_record(expires_in_seconds=300)
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()
        settings.NETSUITE_OAUTH_CLIENT_ID = "global_client"

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.redis_lock.acquire_lock", return_value=False),
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), settings)

        assert stats["skipped_locked"] == 1
        assert stats["refreshed"] == 0

    def test_error_counted_on_refresh_failure(self):
        """Failed refresh should increment error counter."""
        record, creds = _make_record(expires_in_seconds=300)
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()
        settings.NETSUITE_OAUTH_CLIENT_ID = "global_client"

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh", side_effect=Exception("HTTP 400")),
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), settings)

        assert stats["errors"] == 1
        assert stats["refreshed"] == 0

    def test_skips_non_oauth2(self):
        """Non-OAuth2 connections should be skipped."""
        record, creds = _make_record(expires_in_seconds=300, auth_type="oauth1_tba")
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()

        with patch("app.core.encryption.decrypt_credentials", return_value=creds):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), settings)

        assert stats["refreshed"] == 0

    def test_uses_global_client_id_for_rest(self):
        """REST connections should use global NETSUITE_OAUTH_CLIENT_ID."""
        record, creds = _make_record(expires_in_seconds=300)
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()
        settings.NETSUITE_OAUTH_CLIENT_ID = "global_client"

        token_data = {"access_token": "new", "expires_in": 3600}

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.encryption.encrypt_credentials", return_value="enc"),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh", return_value=token_data) as mock_refresh,
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh", stats, datetime.now(timezone.utc), settings)

        # Should use global client_id, not per-connection
        mock_refresh.assert_called_once_with("1234567", "refresh_tok", "global_client")

    def test_uses_stored_client_id_for_mcp(self):
        """MCP connectors should use stored per-connection client_id."""
        record, creds = _make_record(expires_in_seconds=300)
        stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}
        settings = MagicMock()
        settings.NETSUITE_OAUTH_CLIENT_ID = "global_client"

        token_data = {"access_token": "new", "expires_in": 3600}

        with (
            patch("app.core.encryption.decrypt_credentials", return_value=creds),
            patch("app.core.encryption.encrypt_credentials", return_value="enc"),
            patch("app.core.redis_lock.acquire_lock", return_value=True),
            patch("app.core.redis_lock.release_lock"),
            patch("app.workers.tasks.proactive_token_refresh._run_async_refresh", return_value=token_data) as mock_refresh,
        ):
            _refresh_single(MagicMock(), record, "oauth_refresh:mcp", stats, datetime.now(timezone.utc), settings)

        # Should use per-connection client_id for MCP
        mock_refresh.assert_called_once_with("1234567", "refresh_tok", "test_client_id")
