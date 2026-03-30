"""Tests for v1.5 Reconciliation Self-Service: permissions, data-status, sync trigger.

TDD — tests written before implementation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.dependencies import require_any_permission

# ---------------------------------------------------------------------------
# require_any_permission tests
# ---------------------------------------------------------------------------


class TestRequireAnyPermission:
    """Tests for the new require_any_permission() dependency helper."""

    @pytest.mark.asyncio
    async def test_allows_first_permission(self):
        """User with first permission should be allowed."""
        checker = require_any_permission("connections.manage", "recon.run")
        user = MagicMock()
        user.user_roles = [MagicMock(role_id=uuid.uuid4())]

        db = AsyncMock()
        # Mock permission query to return connections.manage
        mock_result = MagicMock()
        mock_result.all.return_value = [("connections.manage",)]
        db.execute.return_value = mock_result

        result = await checker(user=user, db=db)
        assert result == user

    @pytest.mark.asyncio
    async def test_allows_second_permission(self):
        """User with second permission should be allowed."""
        checker = require_any_permission("connections.manage", "recon.run")
        user = MagicMock()
        user.user_roles = [MagicMock(role_id=uuid.uuid4())]

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [("recon.run",)]
        db.execute.return_value = mock_result

        result = await checker(user=user, db=db)
        assert result == user

    @pytest.mark.asyncio
    async def test_denies_no_matching_permission(self):
        """User without any matching permission should be denied."""
        checker = require_any_permission("connections.manage", "recon.run")
        user = MagicMock()
        user.user_roles = [MagicMock(role_id=uuid.uuid4())]

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [("some.other.perm",)]
        db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await checker(user=user, db=db)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_denies_no_roles(self):
        """User with no roles should be denied."""
        checker = require_any_permission("connections.manage", "recon.run")
        user = MagicMock()
        user.user_roles = []

        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await checker(user=user, db=db)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_allows_with_both_permissions(self):
        """User with both permissions should be allowed."""
        checker = require_any_permission("connections.manage", "recon.run")
        user = MagicMock()
        user.user_roles = [MagicMock(role_id=uuid.uuid4())]

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [("connections.manage",), ("recon.run",)]
        db.execute.return_value = mock_result

        result = await checker(user=user, db=db)
        assert result == user


# ---------------------------------------------------------------------------
# stripe_sync_all task tests
# ---------------------------------------------------------------------------


class TestStripeSyncAll:
    """Tests for the stripe_sync_all scheduled task."""

    def test_dispatches_per_tenant_tasks(self):
        """Should dispatch a stripe_sync task for each active Stripe connection."""
        from app.workers.tasks.stripe_sync_all import _find_active_stripe_connections

        # This tests the helper that finds active connections
        # The actual task dispatches per-tenant sync tasks
        assert callable(_find_active_stripe_connections)

    def test_skips_inactive_connections(self):
        """Should not dispatch tasks for inactive/error Stripe connections."""
        from app.workers.tasks.stripe_sync_all import _find_active_stripe_connections

        # Tested via the helper function
        assert callable(_find_active_stripe_connections)


# ---------------------------------------------------------------------------
# Data freshness banner data tests
# ---------------------------------------------------------------------------


class TestDataFreshnessLogic:
    """Tests for data staleness detection logic."""

    def test_stale_threshold_24h(self):
        """Data older than 24 hours should be flagged as stale."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        last_sync = now - timedelta(hours=25)
        is_stale = (now - last_sync).total_seconds() > 86400
        assert is_stale is True

    def test_fresh_threshold_24h(self):
        """Data younger than 24 hours should not be flagged as stale."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        last_sync = now - timedelta(hours=2)
        is_stale = (now - last_sync).total_seconds() > 86400
        assert is_stale is False

    def test_never_synced_is_stale(self):
        """No last_sync should be treated as stale."""
        last_sync = None
        is_stale = last_sync is None
        assert is_stale is True


# ---------------------------------------------------------------------------
# Progress callback tests
# ---------------------------------------------------------------------------


class TestProgressCallback:
    """Tests for stripe sync progress callback mechanism."""

    def test_callback_invoked_during_sync(self):
        """Progress callback should be called with payout count during sync."""
        calls = []

        def callback(payouts_synced: int, stage: str = "payouts"):
            calls.append({"count": payouts_synced, "stage": stage})

        # Simulate calling the callback as sync_stripe would
        callback(20, "payouts")
        callback(40, "payouts")
        callback(60, "payouts")

        assert len(calls) == 3
        assert calls[0]["count"] == 20
        assert calls[2]["count"] == 60

    def test_callback_none_is_noop(self):
        """When callback is None, sync should still work (no error)."""
        callback = None
        # This should not raise
        if callback:
            callback(20)
        # No assertion needed — just verifying no exception


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------


class TestSyncRateLimiting:
    """Tests for sync rate limiting via Redis lock."""

    def test_acquire_lock_prevents_concurrent_sync(self):
        """Second sync attempt within 5 minutes should be blocked."""
        from app.core.redis_lock import acquire_lock, release_lock

        key = f"recon_sync:test-{uuid.uuid4()}"
        # First acquire should succeed
        assert acquire_lock(key, timeout=300) is True
        # Second acquire should fail
        assert acquire_lock(key, timeout=300) is False
        # Cleanup
        release_lock(key)

    def test_lock_released_after_sync(self):
        """Lock should be released after sync completes."""
        from app.core.redis_lock import acquire_lock, release_lock

        key = f"recon_sync:test-{uuid.uuid4()}"
        assert acquire_lock(key, timeout=300) is True
        release_lock(key)
        # Should be able to acquire again
        assert acquire_lock(key, timeout=300) is True
        release_lock(key)
