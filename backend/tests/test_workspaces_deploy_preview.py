"""HTTP-layer tests for the two-step gated SuiteCloud sandbox deploy.

Tests 15-18 in spec:
  15 — POST /deploy-sandbox/preview returns manifest + token
  16 — POST /deploy-sandbox/confirm queues run with expected_snapshot_sha
  17 — POST /deploy-sandbox/confirm marks token consumed_run_id
  18 — Old POST /deploy-sandbox returns 410 Gone
"""

from __future__ import annotations

import contextlib
import hashlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceDeployToken,
    WorkspaceFile,
    WorkspaceRun,
)

_FAKE_MOD_KEY = "app.workers.tasks.workspace_run"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _mock_workspace_run_task():
    """Same pattern as test_phase3c — substitute a fake celery task module
    so tests don't actually try to dispatch to Redis."""
    fake_module = ModuleType(_FAKE_MOD_KEY)
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    fake_module.workspace_run_task = fake_task

    old = sys.modules.get(_FAKE_MOD_KEY)
    sys.modules[_FAKE_MOD_KEY] = fake_module
    try:
        yield fake_task
    finally:
        if old is not None:
            sys.modules[_FAKE_MOD_KEY] = old
        else:
            sys.modules.pop(_FAKE_MOD_KEY, None)


@pytest_asyncio.fixture
async def deploy_eligible_changeset(db: AsyncSession, tenant_a, admin_user):
    """Approved changeset + passing validate/jest runs. Same shape as the
    service-layer fixture."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="HTTP deploy tests WS",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    db.add(
        WorkspaceFile(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            path="SuiteScripts/deploy_me.js",
            file_name="deploy_me.js",
            content="console.log('deploy');",
            sha256_hash=_sha256("console.log('deploy');"),
            size_bytes=22,
            is_directory=False,
        )
    )
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="HTTP deploy test cs",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    db.add(
        WorkspaceRun(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            changeset_id=cs.id,
            run_type="suitecloud_validate",
            status="passed",
            triggered_by=user.id,
            has_errors=False,
            gate_status="pass",
        )
    )
    db.add(
        WorkspaceRun(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            changeset_id=cs.id,
            run_type="jest_unit_test",
            status="passed",
            triggered_by=user.id,
        )
    )
    await db.flush()
    return ws, cs, user


def _auth_headers(user_id, tenant_id) -> dict:
    token = create_access_token({"sub": str(user_id), "tenant_id": str(tenant_id)})
    return {"Authorization": f"Bearer {token}"}


class TestDeployPreviewEndpoint:
    """Test 15: POST /deploy-sandbox/preview returns manifest + token."""

    @pytest.mark.asyncio
    async def test_15_preview_returns_manifest_and_token(
        self, client: AsyncClient, deploy_eligible_changeset, tenant_a
    ):
        ws, cs, user = deploy_eligible_changeset

        response = await client.post(
            f"/api/v1/changesets/{cs.id}/deploy-sandbox/preview",
            json={"sandbox_id": "6738075-sb1", "require_assertions": False},
            headers=_auth_headers(user.id, tenant_a.id),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert "manifest" in body
        assert "snapshot_sha" in body
        assert "manifest_sha" in body
        assert "confirmation_token" in body
        assert "jti" in body
        assert "expires_at" in body
        assert body["sandbox_id"] == "6738075-sb1"
        # At least the one baseline file appears in the manifest.
        manifest_paths = [m["path"] for m in body["manifest"]]
        assert "SuiteScripts/deploy_me.js" in manifest_paths


class TestDeployConfirmEndpoint:
    """Tests 16 + 17: POST /deploy-sandbox/confirm queues run and links token."""

    @pytest.mark.asyncio
    async def test_16_17_confirm_queues_run_and_links_token(
        self, db: AsyncSession, client: AsyncClient, deploy_eligible_changeset, tenant_a
    ):
        ws, cs, user = deploy_eligible_changeset

        # Preview first to mint a token.
        preview_resp = await client.post(
            f"/api/v1/changesets/{cs.id}/deploy-sandbox/preview",
            json={"sandbox_id": "6738075-sb1", "require_assertions": False},
            headers=_auth_headers(user.id, tenant_a.id),
        )
        assert preview_resp.status_code == 200, preview_resp.text
        preview = preview_resp.json()

        with _mock_workspace_run_task() as fake_task:
            confirm_resp = await client.post(
                f"/api/v1/changesets/{cs.id}/deploy-sandbox/confirm",
                json={
                    "jti": preview["jti"],
                    "confirmation_token": preview["confirmation_token"],
                },
                headers=_auth_headers(user.id, tenant_a.id),
            )
            assert confirm_resp.status_code == 202, confirm_resp.text

            # Test 16 — worker dispatch carries expected_snapshot_sha so the
            # runner can re-verify before invoking suitecloud project:deploy.
            assert fake_task.delay.called
            call_kwargs = fake_task.delay.call_args.kwargs
            assert (
                call_kwargs["extra_params"]["expected_snapshot_sha"]
                == preview["snapshot_sha"]
            )
            assert call_kwargs["extra_params"]["sandbox_id"] == "6738075-sb1"

        # Test 17 — token row consumed + consumed_run_id linked.
        # We must re-query in a fresh transaction context because the
        # endpoint committed. Use the same db session.
        # NOTE: The client's request used a separate db session via the
        # dependency override, so refresh here is enough.
        run_id = confirm_resp.json()["id"]

        token_row = await db.execute(
            select(WorkspaceDeployToken).where(
                WorkspaceDeployToken.id == __import__("uuid").UUID(preview["jti"])
            )
        )
        # The dep-overridden db session in the test client is the same as
        # `db` here, so the consumed_at/consumed_run_id updates are visible.
        row = token_row.scalar_one()
        assert row.consumed_at is not None
        assert str(row.consumed_run_id) == run_id


class TestLegacyEndpointGone:
    """Test 18: old one-click endpoint returns 410 Gone."""

    @pytest.mark.asyncio
    async def test_18_old_endpoint_returns_410(
        self, client: AsyncClient, deploy_eligible_changeset, tenant_a
    ):
        ws, cs, user = deploy_eligible_changeset

        response = await client.post(
            f"/api/v1/changesets/{cs.id}/deploy-sandbox",
            json={"sandbox_id": "6738075-sb1"},
            headers=_auth_headers(user.id, tenant_a.id),
        )

        assert response.status_code == 410, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "deploy_one_click_deprecated"
        assert "preview_endpoint" in detail
        assert "confirm_endpoint" in detail
