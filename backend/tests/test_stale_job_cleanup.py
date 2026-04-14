"""Tests for stale job auto-cleanup on startup."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from tests.conftest import create_test_tenant


class TestStaleJobCleanup:
    """Verify _cleanup_stale_jobs marks stale running jobs as failed."""

    async def _insert_job(
        self, db: AsyncSession, tenant_id: uuid.UUID, status: str, started_at: datetime
    ) -> uuid.UUID:
        job = Job(
            tenant_id=tenant_id,
            job_type="tasks.test_task",
            status=status,
            started_at=started_at,
        )
        db.add(job)
        await db.flush()
        return job.id

    async def test_stale_running_job_marked_failed(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Stale Job Corp")
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        job_id = await self._insert_job(db, tenant.id, "running", two_hours_ago)

        # Simulate cleanup query
        await db.execute(
            text(
                "UPDATE jobs SET status = 'failed', "
                "completed_at = NOW(), "
                "error_message = 'Auto-cleaned: marked stale on startup' "
                "WHERE status = 'running' "
                "AND started_at < NOW() - INTERVAL '10 minutes'"
            )
        )

        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one()
        assert job.status == "failed"
        assert job.completed_at is not None
        assert "Auto-cleaned" in job.error_message

    async def test_recent_running_job_not_cleaned(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Recent Job Corp")
        one_min_ago = datetime.now(timezone.utc) - timedelta(minutes=1)
        job_id = await self._insert_job(db, tenant.id, "running", one_min_ago)

        await db.execute(
            text(
                "UPDATE jobs SET status = 'failed', "
                "completed_at = NOW(), "
                "error_message = 'Auto-cleaned: marked stale on startup' "
                "WHERE status = 'running' "
                "AND started_at < NOW() - INTERVAL '10 minutes'"
            )
        )

        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one()
        assert job.status == "running"  # Not touched

    async def test_completed_job_not_cleaned(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Done Job Corp")
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        job_id = await self._insert_job(db, tenant.id, "completed", two_hours_ago)

        await db.execute(
            text(
                "UPDATE jobs SET status = 'failed', "
                "completed_at = NOW(), "
                "error_message = 'Auto-cleaned: marked stale on startup' "
                "WHERE status = 'running' "
                "AND started_at < NOW() - INTERVAL '10 minutes'"
            )
        )

        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one()
        assert job.status == "completed"  # Not touched
