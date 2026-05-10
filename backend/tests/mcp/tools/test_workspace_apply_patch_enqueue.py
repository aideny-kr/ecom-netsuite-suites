"""execute_apply_patch should enqueue an auto-validate run on success.

Task 8 wiring: after a changeset is applied, hand off to
`AutoValidateOrchestrator.enqueue(...)` so a `suitecloud_validate`
run is created. Apply must NOT fail when the orchestrator raises
(e.g. dev container with no `_create_run` closure wired) — the
patch is applied; queue failure is logged but not propagated.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.tools import workspace_tools
from app.models.workspace import WorkspacePatch
from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="ApplyPatch Enqueue", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def user(db, tenant):
    u, _ = await create_test_user(db, tenant, role_name="admin")
    return u


@pytest_asyncio.fixture
async def workspace_with_files(db, tenant, user):
    ws = await ws_svc.create_workspace(db, tenant.id, "ApplyPatch WS", user.id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("src/index.ts", "export const app = 'hello';")
    await ws_svc.import_workspace(db, ws.id, tenant.id, buf.getvalue())
    return ws


async def _approved_create_changeset(db, tenant, user, workspace):
    """Build an approved changeset with a single 'create' patch (no diff conflicts)."""
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Apply+Enqueue CS", user.id)
    patch_row = WorkspacePatch(
        tenant_id=tenant.id,
        changeset_id=cs.id,
        file_path="generated.txt",
        operation="create",
        new_content="hello\n",
        baseline_sha256="",
        apply_order=0,
    )
    db.add(patch_row)
    await db.flush()
    await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)
    return cs


@pytest.mark.asyncio
async def test_apply_patch_success_enqueues_validate(db, tenant, user, workspace_with_files) -> None:
    """Successful apply hands the changeset to AutoValidateOrchestrator.enqueue()."""
    cs = await _approved_create_changeset(db, tenant, user, workspace_with_files)

    enqueue_mock = AsyncMock(return_value=uuid.uuid4())
    fake_orchestrator = MagicMock()
    fake_orchestrator.enqueue = enqueue_mock

    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator",
        return_value=fake_orchestrator,
    ):
        result = await workspace_tools.execute_apply_patch(
            params={"changeset_id": str(cs.id)},
            context={
                "tenant_id": str(tenant.id),
                "actor_id": str(user.id),
                "db": db,
            },
        )

    assert "error" not in result, f"unexpected error: {result.get('error')}"
    assert result["changeset_id"] == str(cs.id)
    assert result["status"] == "applied"

    enqueue_mock.assert_awaited_once()
    kwargs = enqueue_mock.call_args.kwargs
    assert kwargs["workspace_id"] == workspace_with_files.id
    assert kwargs["changeset_id"] == cs.id
    assert kwargs["tenant_id"] == tenant.id
    assert kwargs["triggered_by"] == user.id


@pytest.mark.asyncio
async def test_apply_patch_failure_does_not_enqueue(db, tenant, user, workspace_with_files) -> None:
    """Draft changeset → apply_changeset raises ValueError → no enqueue, error returned."""
    cs = await ws_svc.create_changeset(db, workspace_with_files.id, tenant.id, "Draft CS", user.id)
    # Intentionally do NOT submit/approve — apply_changeset will raise ValueError.

    enqueue_mock = AsyncMock()
    fake_orchestrator = MagicMock()
    fake_orchestrator.enqueue = enqueue_mock

    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator",
        return_value=fake_orchestrator,
    ):
        result = await workspace_tools.execute_apply_patch(
            params={"changeset_id": str(cs.id)},
            context={
                "tenant_id": str(tenant.id),
                "actor_id": str(user.id),
                "db": db,
            },
        )

    assert "error" in result
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_patch_orchestrator_failure_does_not_break_apply(db, tenant, user, workspace_with_files) -> None:
    """If enqueue raises (e.g. _create_run not wired), the apply still succeeds."""
    cs = await _approved_create_changeset(db, tenant, user, workspace_with_files)

    enqueue_mock = AsyncMock(side_effect=RuntimeError("orchestrator not initialized"))
    fake_orchestrator = MagicMock()
    fake_orchestrator.enqueue = enqueue_mock

    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator",
        return_value=fake_orchestrator,
    ):
        result = await workspace_tools.execute_apply_patch(
            params={"changeset_id": str(cs.id)},
            context={
                "tenant_id": str(tenant.id),
                "actor_id": str(user.id),
                "db": db,
            },
        )

    assert "error" not in result, f"apply should not fail when enqueue raises: {result.get('error')}"
    assert result["status"] == "applied"
    enqueue_mock.assert_awaited_once()
