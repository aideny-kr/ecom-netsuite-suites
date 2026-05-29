"""A workspace-path collision during suitescript sync must skip the colliding
file (counted as failed), not wedge the whole sync.

Regression for the 2026-05-28 incident: ``_upsert_workspace_file`` moved a file
onto a ``path`` already held by another row -> UniqueViolation on
``uq_workspace_files_workspace_path`` with no per-file rollback -> the session's
transaction went into PendingRollbackError and every subsequent file in the sync
failed.
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.services.suitescript_sync_service as svc
from app.models.workspace import Workspace, WorkspaceFile
from tests.conftest import create_test_tenant, create_test_user


async def test_upsert_file_resilient_skips_path_collision(db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    ws = Workspace(tenant_id=tenant.id, name="ws", created_by=user.id)
    db.add(ws)
    await db.flush()

    dup_path = "SuiteScripts/Other/moment-with-locales.js"

    # File B (netsuite id 2) already occupies dup_path.
    assert (
        await svc._upsert_file_resilient(db, tenant.id, ws.id, dup_path, "B", netsuite_file_id="2", script_type="Other")
        is True
    )
    # File A (netsuite id 1) lives elsewhere.
    assert (
        await svc._upsert_file_resilient(
            db, tenant.id, ws.id, "SuiteScripts/Other/a.js", "A", netsuite_file_id="1", script_type="Other"
        )
        is True
    )
    await db.flush()

    # A is reorganized onto dup_path (already held by B) -> path collision.
    # Must skip (return False), NOT raise, and leave the session usable.
    loaded = await svc._upsert_file_resilient(
        db, tenant.id, ws.id, dup_path, "A-moved", netsuite_file_id="1", script_type="Other"
    )
    assert loaded is False

    # Session still usable and no rows lost: both files remain.
    count = (
        await db.execute(select(func.count()).select_from(WorkspaceFile).where(WorkspaceFile.workspace_id == ws.id))
    ).scalar()
    assert count == 2


async def test_upsert_file_resilient_reraises_unrelated_integrity_error(db):
    """Only path collisions are swallowed; other integrity errors must propagate."""
    tenant = await create_test_tenant(db)
    bogus_workspace_id = uuid.uuid4()  # no such workspace -> FK violation, not a path collision

    with pytest.raises(IntegrityError):
        await svc._upsert_file_resilient(
            db,
            tenant.id,
            bogus_workspace_id,
            "SuiteScripts/Other/x.js",
            "X",
            netsuite_file_id="9",
            script_type="Other",
        )
