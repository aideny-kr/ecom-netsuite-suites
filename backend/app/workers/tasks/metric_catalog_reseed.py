"""Celery Beat task: (re)seed SYSTEM-default metric definitions.

A fresh/staging DB must ship a populated metric catalog — the standalone
``app/scripts/seed_metric_catalog.py`` is never invoked on deploy, so this task
runs ``seed_system_metrics`` on a schedule. The seeder ensures the SYSTEM tenant
parent row and DELETE-then-INSERTs the SYSTEM metric rows, so it is idempotent
and safe to run repeatedly. DAILY is sufficient for static system metrics.

Mirrors the structure of ``app/workers/tasks/oracle_skill_reseed.py``.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.database import async_session_factory
from app.services.metrics.metric_catalog_seeder import seed_system_metrics
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.metric_catalog_reseed")
def reseed_system_metrics_task() -> dict:
    """Celery entrypoint. Runs daily per Beat schedule. Idempotent."""

    async def _run():
        async with async_session_factory() as db:
            try:
                count = await seed_system_metrics(db)
                await db.commit()
                return {"status": "ok", "seeded": count}
            except Exception as e:
                logger.exception("Metric catalog reseed task failed")
                await db.rollback()
                return {"status": "error", "error": str(e)}

    return asyncio.run(_run())
