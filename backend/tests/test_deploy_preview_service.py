"""Tests for deploy_preview_service — two-step gated API for sandbox deploy.

Spec: docs/superpowers/specs/2026-05-18-suitecloud-sandbox-deploy-gated-api.md

Test plan (28 backend + frontend tests):
  1. compute_deploy_manifest returns sorted entries with stable snapshot_sha/manifest_sha
  2. Two patches producing same final tree but different apply_order yield same snapshot_sha,
     different manifest_sha
  3-14. build_deploy_preview + verify_and_consume_deploy_token (backend service)
  15-18. HTTP API endpoints
  19-20. Worker re-verification
  21-24. MCP tool changes
  25-28. Frontend (separate file)
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspacePatch,
)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def workspace_with_files(db: AsyncSession, tenant_a, admin_user):
    """Workspace + 2 baseline files + approved changeset with no patches yet."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Deploy Preview WS",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    f1 = WorkspaceFile(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        path="SuiteScripts/keep.js",
        file_name="keep.js",
        content="console.log('keep');",
        sha256_hash=_sha256("console.log('keep');"),
        size_bytes=20,
        is_directory=False,
    )
    f2 = WorkspaceFile(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        path="SuiteScripts/modme.js",
        file_name="modme.js",
        content="// original\n",
        sha256_hash=_sha256("// original\n"),
        size_bytes=12,
        is_directory=False,
    )
    db.add_all([f1, f2])

    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="Deploy preview test changeset",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()
    return ws, cs, user


# ---------------------------------------------------------------------------
# Test 1: stable hashes from compute_deploy_manifest
# ---------------------------------------------------------------------------


class TestComputeDeployManifest:
    """Test 1 in spec: compute_deploy_manifest returns sorted entries with stable hashes."""

    @pytest.mark.asyncio
    async def test_stable_hashes_no_patches(self, db: AsyncSession, workspace_with_files, tenant_a):
        """No patches → manifest reflects baseline files; hashes are deterministic across calls."""
        from app.services.deploy_preview_service import compute_deploy_manifest

        ws, cs, _ = workspace_with_files

        result_1 = await compute_deploy_manifest(
            db=db, changeset_id=cs.id, tenant_id=tenant_a.id, workspace_id=ws.id
        )
        result_2 = await compute_deploy_manifest(
            db=db, changeset_id=cs.id, tenant_id=tenant_a.id, workspace_id=ws.id
        )

        # Manifest is sorted by path so order is stable.
        manifest_paths = [entry["path"] for entry in result_1["manifest"]]
        assert manifest_paths == sorted(manifest_paths), "manifest must be sorted by path"

        # Two consecutive calls return identical hashes.
        assert result_1["snapshot_sha"] == result_2["snapshot_sha"]
        assert result_1["manifest_sha"] == result_2["manifest_sha"]

        # Hashes are 64-hex sha256 strings.
        assert len(result_1["snapshot_sha"]) == 64
        assert len(result_1["manifest_sha"]) == 64
        assert all(c in "0123456789abcdef" for c in result_1["snapshot_sha"])

    @pytest.mark.asyncio
    async def test_stable_hashes_with_patches(
        self, db: AsyncSession, workspace_with_files, tenant_a
    ):
        """create + modify + delete patches → manifest reflects post-patch tree."""
        from app.services.deploy_preview_service import compute_deploy_manifest

        ws, cs, _ = workspace_with_files

        # create — new file
        db.add(
            WorkspacePatch(
                tenant_id=tenant_a.id,
                changeset_id=cs.id,
                file_path="Objects/customscript_x.xml",
                operation="create",
                new_content="<scriptdeployment/>",
                baseline_sha256="",
                apply_order=1,
            )
        )
        # modify — bumps modme.js
        db.add(
            WorkspacePatch(
                tenant_id=tenant_a.id,
                changeset_id=cs.id,
                file_path="SuiteScripts/modme.js",
                operation="modify",
                new_content="// modified\n",
                baseline_sha256=_sha256("// original\n"),
                apply_order=2,
            )
        )
        await db.flush()

        result = await compute_deploy_manifest(
            db=db, changeset_id=cs.id, tenant_id=tenant_a.id, workspace_id=ws.id
        )

        # Manifest entries: 2 originals (one of which is "modify"-touched) + 1 new file.
        paths = {entry["path"] for entry in result["manifest"]}
        assert "Objects/customscript_x.xml" in paths
        assert "SuiteScripts/modme.js" in paths
        assert "SuiteScripts/keep.js" in paths

        # Verify operations are surfaced for create/modify patches.
        ops_by_path = {entry["path"]: entry["operation"] for entry in result["manifest"]}
        assert ops_by_path["Objects/customscript_x.xml"] == "create"
        assert ops_by_path["SuiteScripts/modme.js"] == "modify"
        # untouched files should appear with operation "unchanged"
        assert ops_by_path["SuiteScripts/keep.js"] == "unchanged"

        # Manifest entries each carry content_sha + apply_order.
        for entry in result["manifest"]:
            assert "content_sha" in entry
            assert "apply_order" in entry
            assert len(entry["content_sha"]) == 64


# ---------------------------------------------------------------------------
# Test 2: snapshot_sha vs manifest_sha distinguish patch ordering
# ---------------------------------------------------------------------------


class TestManifestShaDistinguishesOrder:
    """Test 2 in spec: two patch sequences producing the same final tree but
    different apply_order yield the same snapshot_sha but different
    manifest_sha. snapshot_sha collapses on the post-image; manifest_sha
    catches the intermediate state and operation history.
    """

    @pytest.mark.asyncio
    async def test_same_final_tree_different_order_distinct_manifest_sha(
        self, db: AsyncSession, tenant_a, admin_user
    ):
        """Two separate changesets land the same file content via different
        apply_order. snapshot_sha matches; manifest_sha differs."""
        from app.services.deploy_preview_service import compute_deploy_manifest

        user, _ = admin_user

        ws = Workspace(
            tenant_id=tenant_a.id,
            name="Manifest order WS",
            created_by=user.id,
            status="active",
        )
        db.add(ws)
        await db.flush()

        # Two approved changesets in the same workspace. Each ends with two
        # files in identical final state, but the patches that get them there
        # use different apply_order assignments.
        cs_a = WorkspaceChangeSet(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            title="cs_a",
            status="approved",
            proposed_by=user.id,
        )
        cs_b = WorkspaceChangeSet(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            title="cs_b",
            status="approved",
            proposed_by=user.id,
        )
        db.add_all([cs_a, cs_b])
        await db.flush()

        # Two new files, same final content in both changesets.
        for cs, orders in ((cs_a, (1, 2)), (cs_b, (2, 1))):
            db.add(
                WorkspacePatch(
                    tenant_id=tenant_a.id,
                    changeset_id=cs.id,
                    file_path="SuiteScripts/a.js",
                    operation="create",
                    new_content="console.log('a');",
                    baseline_sha256="",
                    apply_order=orders[0],
                )
            )
            db.add(
                WorkspacePatch(
                    tenant_id=tenant_a.id,
                    changeset_id=cs.id,
                    file_path="SuiteScripts/b.js",
                    operation="create",
                    new_content="console.log('b');",
                    baseline_sha256="",
                    apply_order=orders[1],
                )
            )
        await db.flush()

        result_a = await compute_deploy_manifest(
            db=db, changeset_id=cs_a.id, tenant_id=tenant_a.id, workspace_id=ws.id
        )
        result_b = await compute_deploy_manifest(
            db=db, changeset_id=cs_b.id, tenant_id=tenant_a.id, workspace_id=ws.id
        )

        # Same final tree → same snapshot_sha.
        assert result_a["snapshot_sha"] == result_b["snapshot_sha"], (
            "Identical post-patch tree must yield identical snapshot_sha"
        )

        # Different apply_order → different manifest_sha.
        assert result_a["manifest_sha"] != result_b["manifest_sha"], (
            "Different apply_order must yield different manifest_sha so the "
            "operator-reviewed manifest is cryptographically distinguishable"
        )
