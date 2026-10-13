"""Manual edits remain proposals and bind to the exact file the builder read."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.models.workspace import WorkspaceChangeSet, WorkspaceFile, WorkspacePatch
from app.services import workspace_service as svc
from tests import test_workspace as fixtures

tenant = fixtures.tenant
user = fixtures.user
workspace = fixtures.workspace


def sha(content):
    return hashlib.sha256(content.encode()).hexdigest()


async def seed(db, workspace, tenant):
    file = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=workspace.id,
        path="src/demo.js",
        file_name="demo.js",
        content="old\n",
        sha256_hash=sha("old\n"),
        size_bytes=4,
        is_directory=False,
    )
    db.add(file)
    await db.flush()
    return file


@pytest.mark.asyncio
async def test_edit_creates_reviewable_draft_without_changing_file(db, workspace, tenant, user):
    file = await seed(db, workspace, tenant)
    result = await svc.propose_file_edit(db, workspace.id, file.id, tenant.id, user.id, sha("old\n"), "new\n")
    cs = await svc.get_changeset(db, uuid.UUID(result["changeset_id"]), tenant.id)
    assert cs.status == "draft"
    assert file.content == "old\n"
    diff = await svc.get_changeset_diff(db, cs.id, tenant.id)
    assert diff["files"][0]["modified_content"] == "new\n"
    assert diff["files"][0]["original_content"] == "old\n"
    await svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    await svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)
    await svc.apply_changeset(db, cs.id, tenant.id, user.id)
    assert file.content == "new\n"


@pytest.mark.asyncio
async def test_stale_editor_does_not_propose_or_lock(db, workspace, tenant, user):
    file = await seed(db, workspace, tenant)
    with pytest.raises(ValueError, match="changed"):
        await svc.propose_file_edit(db, workspace.id, file.id, tenant.id, user.id, sha("older"), "new")
    assert file.locked_by is None
    assert (
        await db.execute(select(WorkspaceChangeSet).where(WorkspaceChangeSet.workspace_id == workspace.id))
    ).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_scope", ["tenant", "workspace", "file"])
async def test_edit_rejects_foreign_scope(db, workspace, tenant, user, wrong_scope):
    file = await seed(db, workspace, tenant)
    args = [workspace.id, file.id, tenant.id]
    args[{"workspace": 0, "file": 1, "tenant": 2}[wrong_scope]] = uuid.uuid4()
    with pytest.raises(ValueError, match="not found"):
        await svc.propose_file_edit(db, *args, user.id, sha("old\n"), "new")
    assert file.content == "old\n"


@pytest.mark.asyncio
async def test_edit_rejects_archived_workspace(db, workspace, tenant, user):
    file = await seed(db, workspace, tenant)
    workspace.status = "archived"
    await db.flush()
    with pytest.raises(ValueError, match="not found"):
        await svc.propose_file_edit(db, workspace.id, file.id, tenant.id, user.id, sha("old\n"), "new")


@pytest.mark.asyncio
async def test_edit_respects_live_lock(db, workspace, tenant, user):
    from datetime import datetime, timezone

    from tests.conftest import create_test_user

    other, _ = await create_test_user(db, tenant, role_name="admin")
    file = await seed(db, workspace, tenant)
    file.locked_by = other.id
    file.locked_at = datetime.now(timezone.utc)
    await db.flush()
    with pytest.raises(ValueError, match="locked"):
        await svc.propose_file_edit(db, workspace.id, file.id, tenant.id, user.id, sha("old\n"), "new")


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["old\n", "é" * (svc.MAX_FILE_SIZE // 2 + 1)], ids=["unchanged", "oversize_utf8"])
async def test_unchanged_and_oversized_edits_rejected(db, workspace, tenant, user, content):
    file = await seed(db, workspace, tenant)
    with pytest.raises(ValueError):
        await svc.propose_file_edit(db, workspace.id, file.id, tenant.id, user.id, sha("old\n"), content)
    assert (await db.execute(select(WorkspacePatch).where(WorkspacePatch.tenant_id == tenant.id))).scalars().all() == []


@pytest.mark.asyncio
async def test_read_returns_revision_even_for_truncated_content(db, workspace, tenant, user):
    file = await seed(db, workspace, tenant)
    file.content = "z" * (svc.MAX_READ_CHARS + 1)
    file.sha256_hash = sha(file.content)
    await db.flush()
    result = await svc.read_file(db, workspace.id, file.id, tenant.id)
    assert result["truncated"] is True
    assert result["sha256_hash"] == sha(file.content)


@pytest.mark.asyncio
async def test_http_edit_requires_current_permission_and_audits_no_content(client, db, workspace, tenant, user):
    from app.core.security import create_access_token
    from app.models.audit import AuditEvent
    from tests.conftest import create_test_user

    file = await seed(db, workspace, tenant)
    token = create_access_token({"sub": str(user.id), "tenant_id": str(tenant.id)})
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/workspaces/{workspace.id}/files/{file.id}"
    read = await client.get(path, headers=headers)
    assert read.status_code == 200
    assert read.json()["can_edit"] is True
    body = {"baseline_sha256": sha("old\n"), "content": "new\n"}
    saved = await client.post(path + "/draft", headers=headers, json=body)
    assert saved.status_code == 201, saved.text
    events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "workspace.file.edit_proposed", AuditEvent.tenant_id == tenant.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert "content" not in events[0].payload
    assert file.content == "old\n"
    reader, _ = await create_test_user(db, tenant, role_name="readonly")
    headers = {"Authorization": "Bearer " + create_access_token({"sub": str(reader.id), "tenant_id": str(tenant.id)})}
    read = await client.get(path, headers=headers)
    assert read.status_code == 200
    assert read.json()["can_edit"] is False
    denied = await client.post(path + "/draft", headers=headers, json=body)
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_apply_and_diff_detect_content_drift_with_stale_cached_hash(db, workspace, tenant, user):
    file = await seed(db, workspace, tenant)
    saved = await svc.propose_file_edit(db, workspace.id, file.id, tenant.id, user.id, sha("old\n"), "new\n")
    cs_id = uuid.UUID(saved["changeset_id"])
    # A source pull can change bytes without updating the cached hash.
    file.content = "pulled after draft\n"
    await db.flush()
    diff = await svc.get_changeset_diff(db, cs_id, tenant.id)
    assert diff["files"][0]["baseline_drift"] is True
    assert diff["files"][0]["diff_status"] == "stale"
    await svc.transition_changeset(db, cs_id, tenant.id, "submit", user.id)
    await svc.transition_changeset(db, cs_id, tenant.id, "approve", user.id)
    with pytest.raises(ValueError, match="Conflict"):
        await svc.apply_changeset(db, cs_id, tenant.id, user.id)
    assert file.content == "pulled after draft\n"


@pytest.mark.asyncio
async def test_partial_line_read_cannot_be_presented_as_complete(db, workspace, tenant, user):
    file = await seed(db, workspace, tenant)
    file.content = "first\nsecond\n"
    await db.flush()
    result = await svc.read_file(db, workspace.id, file.id, tenant.id, line_start=2)
    assert result["truncated"] is True
