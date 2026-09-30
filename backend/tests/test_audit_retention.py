"""Tests for audit log retention."""

import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.models.audit import AuditEvent
from app.services.audit_retention import get_retention_cutoff, get_retention_stats


class TestRetentionCutoff:
    def test_cutoff_is_in_the_past(self):
        cutoff = get_retention_cutoff()
        assert cutoff < datetime.now(timezone.utc)

    def test_cutoff_matches_config(self):
        cutoff = get_retention_cutoff()
        expected = datetime.now(timezone.utc) - timedelta(days=settings.AUDIT_RETENTION_DAYS)
        # Allow 1 second tolerance
        assert abs((cutoff - expected).total_seconds()) < 1


class TestRetentionStats:
    async def test_stats_returns_counts(self, db):
        """Stats endpoint returns total and archivable counts."""
        tenant_id = uuid.uuid4()

        # Add a recent event
        db.add(
            AuditEvent(
                tenant_id=tenant_id,
                category="test",
                action="test.recent",
                actor_type="system",
                status="success",
            )
        )
        await db.flush()

        stats = await get_retention_stats(db, tenant_id=tenant_id)
        assert stats["total_events"] >= 1
        assert stats["archivable_events"] >= 0
        assert stats["retention_days"] == settings.AUDIT_RETENTION_DAYS
        assert "cutoff_date" in stats

    async def test_old_events_counted_as_archivable(self, db):
        """Events older than retention period are counted as archivable."""
        tenant_id = uuid.uuid4()
        old_timestamp = datetime.now(timezone.utc) - timedelta(days=settings.AUDIT_RETENTION_DAYS + 1)

        db.add(
            AuditEvent(
                tenant_id=tenant_id,
                timestamp=old_timestamp,
                category="test",
                action="test.old",
                actor_type="system",
                status="success",
            )
        )
        await db.flush()

        stats = await get_retention_stats(db, tenant_id=tenant_id)
        assert stats["archivable_events"] >= 1


async def test_retention_preserves_durable_review_stop(db):
    from sqlalchemy import select

    from app.services.audit_retention import purge_old_events_sync

    tenant = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=settings.AUDIT_RETENTION_DAYS + 1)
    stop = AuditEvent(
        tenant_id=tenant,
        timestamp=old,
        category="transaction_ops",
        action="transaction_ops.review.stopped",
        resource_type="transaction_review",
        resource_id=str(uuid.uuid4()),
    )
    ordinary = AuditEvent(tenant_id=tenant, timestamp=old, category="test", action="test.old")
    db.add_all([stop, ordinary])
    await db.flush()
    stop_id, ordinary_id = stop.id, ordinary.id
    stats = await get_retention_stats(db, tenant)
    assert stats["archivable_events"] == 1
    await db.run_sync(purge_old_events_sync)
    assert await db.scalar(select(AuditEvent.id).where(AuditEvent.id == stop_id)) == stop_id
    assert await db.scalar(select(AuditEvent.id).where(AuditEvent.id == ordinary_id)) is None
