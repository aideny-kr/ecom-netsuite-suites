"""Tests for the Beat task that re-seeds SYSTEM metric definitions on deploy.

A fresh/staging DB must not ship an empty metric catalog (spec acceptance #2).
The standalone ``app/scripts/seed_metric_catalog.py`` is never invoked on deploy,
so we mirror the established oracle-reseed pattern: a Celery task + Beat entry
that calls ``seed_system_metrics`` (idempotent: DELETE-then-INSERT of SYSTEM rows).
"""

from unittest.mock import AsyncMock, MagicMock, patch


class TestMetricCatalogReseedTask:
    def test_task_invokes_seeder_and_commits(self):
        """The Celery entrypoint opens a session, calls seed_system_metrics, commits."""
        from app.workers.tasks import metric_catalog_reseed as mod

        mock_db = AsyncMock()
        # async_session_factory() returns an async-context-manager yielding mock_db.
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=mock_db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        seeder = AsyncMock(return_value=9)

        with (
            patch.object(mod, "async_session_factory", return_value=session_cm),
            patch.object(mod, "seed_system_metrics", seeder),
        ):
            result = mod.reseed_system_metrics_task()

        seeder.assert_awaited_once_with(mock_db)
        mock_db.commit.assert_awaited_once()
        assert result == {"status": "ok", "seeded": 9}

    def test_task_rolls_back_on_error(self):
        """Seeder failure → rollback + error status (no crash)."""
        from app.workers.tasks import metric_catalog_reseed as mod

        mock_db = AsyncMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=mock_db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        seeder = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(mod, "async_session_factory", return_value=session_cm),
            patch.object(mod, "seed_system_metrics", seeder),
        ):
            result = mod.reseed_system_metrics_task()

        mock_db.rollback.assert_awaited_once()
        mock_db.commit.assert_not_awaited()
        assert result["status"] == "error"
        assert "boom" in result["error"]

    def test_beat_schedule_contains_entry_pointing_at_task(self):
        """celery_app's beat schedule registers the reseed task name."""
        from app.workers.celery_app import celery_app

        schedule = celery_app.conf.beat_schedule
        entries = [v for v in schedule.values() if v.get("task") == "tasks.metric_catalog_reseed"]
        assert entries, "beat_schedule must contain an entry for tasks.metric_catalog_reseed"

        # And the task module must be on the include list so Beat can resolve it.
        assert "app.workers.tasks.metric_catalog_reseed" in celery_app.conf.include

    def test_task_registered_with_celery(self):
        """The task is registered under the expected name."""
        import app.workers.tasks.metric_catalog_reseed  # noqa: F401  (registers task)
        from app.workers.celery_app import celery_app

        assert "tasks.metric_catalog_reseed" in celery_app.tasks
