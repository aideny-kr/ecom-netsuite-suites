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
        db.add(AuditEvent(
            tenant_id=tenant_id,
            category="test",
            action="test.recent",
            actor_type="system",
            status="success",
        ))
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

        db.add(AuditEvent(
            tenant_id=tenant_id,
            timestamp=old_timestamp,
            category="test",
            action="test.old",
            actor_type="system",
            status="success",
        ))
        await db.flush()

        stats = await get_retention_stats(db, tenant_id=tenant_id)
        assert stats["archivable_events"] >= 1
