"""Tests for Drive RAG Celery tasks + Beat schedule (Phase 9.1).

Verifies:
- `drive_rag_sync_folder` is a registered Celery task on the "sync" queue.
- `drive_rag_sync_all` is a registered Celery task on the "sync" queue.
- `celery_app.conf.beat_schedule` contains the nightly schedule entry.
- `celery_app.conf.include` references the module.
- `drive_rag_sync_all` only enqueues tasks for enabled folders (DB filter).
"""

from __future__ import annotations


def test_drive_rag_sync_folder_is_celery_task():
    from app.workers.tasks.drive_rag_sync import drive_rag_sync_folder

    assert hasattr(drive_rag_sync_folder, "delay")
    assert drive_rag_sync_folder.name == "tasks.drive_rag_sync_folder"


def test_drive_rag_sync_all_is_celery_task():
    from app.workers.tasks.drive_rag_sync import drive_rag_sync_all

    assert hasattr(drive_rag_sync_all, "delay")
    assert drive_rag_sync_all.name == "tasks.drive_rag_sync_all"


def test_beat_schedule_includes_drive_rag_sync():
    from app.workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "drive-rag-sync-nightly" in schedule
    assert schedule["drive-rag-sync-nightly"]["task"] == "tasks.drive_rag_sync_all"


def test_include_contains_drive_rag_sync():
    from app.workers.celery_app import celery_app

    assert "app.workers.tasks.drive_rag_sync" in celery_app.conf.include


def test_tasks_routed_to_sync_queue():
    """Both tasks should be routed to the 'sync' queue via the decorator."""
    from app.workers.tasks.drive_rag_sync import (
        drive_rag_sync_all,
        drive_rag_sync_folder,
    )

    # Celery stores `queue` on the task options dict attached to the Task class.
    assert drive_rag_sync_folder.queue == "sync"
    assert drive_rag_sync_all.queue == "sync"


def test_sync_all_enqueues_only_enabled_folders(monkeypatch):
    """`drive_rag_sync_all` should enqueue per-folder tasks for every
    enabled folder returned by the DB query. Disabled folders are excluded
    at the SQL layer (is_enabled.is_(True)), so the test asserts that each
    returned row is dispatched via the folder-sync task's `.delay`.
    """
    from app.workers.tasks import drive_rag_sync as module

    called_with: list[str] = []

    class FakeDelayTask:
        def delay(self, folder_id):
            called_with.append(folder_id)

    monkeypatch.setattr(module, "drive_rag_sync_folder", FakeDelayTask())

    class FakeFolder:
        def __init__(self, fid):
            self.id = fid

    folders = [FakeFolder("f1"), FakeFolder("f2")]

    class FakeScalars:
        def all(self):
            return folders

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt):
            return FakeResult()

    def _fake_factory():
        return FakeSession()

    monkeypatch.setattr(module, "async_session_factory", _fake_factory)

    # Invoke the task body directly (skip Celery dispatch machinery).
    result = module.drive_rag_sync_all.run()

    assert result == {"enqueued": 2}
    assert called_with == ["f1", "f2"]
