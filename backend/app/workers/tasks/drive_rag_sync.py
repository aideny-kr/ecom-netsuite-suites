"""Celery tasks for Drive RAG sync. Stub — full impl in Phase 9.1."""

from app.workers.celery_app import celery_app


@celery_app.task(name="tasks.drive_rag_sync_folder", queue="sync")
def drive_rag_sync_folder(folder_id: str) -> dict:
    # Implementation added in Phase 9.1
    return {"stub": True, "folder_id": folder_id}
