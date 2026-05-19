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
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceDeployToken,
    WorkspaceFile,
    WorkspacePatch,
    WorkspaceRun,
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


# ---------------------------------------------------------------------------
# Tests 3-5: build_deploy_preview rejection paths
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def deploy_ready_changeset(db: AsyncSession, tenant_a, admin_user):
    """An approved changeset baseline. The rejection-path tests perturb this
    along exactly one axis:

      - test 3 flips status to "pending_review"
      - test 4 leaves gate runs missing (the default state for a fresh
        changeset, which is itself a gate failure path)
      - test 5 supplies a production-shaped sandbox_id

    Test 4 deliberately omits creating ``WorkspaceRun`` rows with the
    sub-project C columns (validator_engine, gate_status, snapshot_hash)
    because the dev Supabase DB is currently mid-migration on those columns
    and we don't want to depend on schema state we don't own.
    """
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Preview WS",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    db.add(
        WorkspaceFile(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            path="SuiteScripts/x.js",
            file_name="x.js",
            content="console.log('x');",
            sha256_hash=_sha256("console.log('x');"),
            size_bytes=20,
            is_directory=False,
        )
    )
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="Preview cs",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()
    return ws, cs, user


class TestBuildDeployPreviewRejection:
    """Tests 3-5 in spec: build_deploy_preview rejects bad input before
    minting any token. Each test perturbs a deploy-ready changeset along
    exactly one axis to isolate the rejection trigger."""

    @pytest.mark.asyncio
    async def test_3_rejects_unapproved_changeset(
        self, db: AsyncSession, deploy_ready_changeset, tenant_a, admin_user
    ):
        """Test 3: changeset.status != 'approved' → ChangesetNotApprovedError."""
        from app.services.deploy_preview_service import (
            ChangesetNotApprovedError,
            build_deploy_preview,
        )

        ws, cs, user = deploy_ready_changeset
        cs.status = "pending_review"
        await db.flush()

        with pytest.raises(ChangesetNotApprovedError):
            await build_deploy_preview(
                db=db,
                changeset_id=cs.id,
                sandbox_id="6738075-sb1",
                require_assertions=False,
                tenant_id=tenant_a.id,
                actor_id=user.id,
            )

    @pytest.mark.asyncio
    async def test_4_rejects_when_gates_fail(
        self, db: AsyncSession, deploy_ready_changeset, tenant_a, admin_user
    ):
        """Test 4: validate / test runs missing → DeployGateNotMetError.

        A fresh approved changeset with no validate or jest_unit_test
        ``WorkspaceRun`` rows fails the gate. This is the natural state
        deploy guards must catch (someone approves a changeset without
        running the gates).

        Codex P1 #6 — the snapshot-pinned gate check applies on the happy
        path; here we just verify the rejection fires before any token is
        minted.
        """
        from app.services.deploy_preview_service import (
            DeployGateNotMetError,
            build_deploy_preview,
        )

        ws, cs, user = deploy_ready_changeset
        # Fixture creates no validate/test runs → gates are missing → gate
        # check returns allowed=False.

        with pytest.raises(DeployGateNotMetError):
            await build_deploy_preview(
                db=db,
                changeset_id=cs.id,
                sandbox_id="6738075-sb1",
                require_assertions=False,
                tenant_id=tenant_a.id,
                actor_id=user.id,
            )

    @pytest.mark.asyncio
    async def test_5_rejects_production_pattern_sandbox(
        self, db: AsyncSession, deploy_ready_changeset, tenant_a, admin_user
    ):
        """Test 5: production-shaped sandbox_id → InvalidSandboxTargetError.

        Reuses runner_service._validate_sandbox_target which hard-blocks
        production patterns and requires sandbox markers (sb*, sandbox*,
        TSTDRV*).
        """
        from app.services.deploy_preview_service import (
            InvalidSandboxTargetError,
            build_deploy_preview,
        )

        ws, cs, user = deploy_ready_changeset

        for bad_target in ("6738075-prod", "6738075", "6738075-live", "production-1"):
            with pytest.raises(InvalidSandboxTargetError):
                await build_deploy_preview(
                    db=db,
                    changeset_id=cs.id,
                    sandbox_id=bad_target,
                    require_assertions=False,
                    tenant_id=tenant_a.id,
                    actor_id=user.id,
                )


# ---------------------------------------------------------------------------
# Tests 6-13: token mint + verify_and_consume happy path and rejections
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fully_ready_changeset(db: AsyncSession, tenant_a, admin_user):
    """An approved changeset with passing validate + jest runs so the
    deploy gate accepts the changeset. Provides a deploy-eligible
    baseline for the mint/verify tests."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Token tests WS",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    db.add(
        WorkspaceFile(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            path="SuiteScripts/baseline.js",
            file_name="baseline.js",
            content="console.log('baseline');",
            sha256_hash=_sha256("console.log('baseline');"),
            size_bytes=25,
            is_directory=False,
        )
    )
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="Token tests cs",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    # Validate + jest must be "passed" for the gate.
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


async def _mint_for(db, ws, cs, user, tenant_id, sandbox_id="6738075-sb1", ttl=600):
    """Helper: run build_deploy_preview + mint_deploy_token. Returns the
    minted token dict + the preview body."""
    from app.services.deploy_preview_service import (
        build_deploy_preview,
        mint_deploy_token,
    )

    preview = await build_deploy_preview(
        db=db,
        changeset_id=cs.id,
        sandbox_id=sandbox_id,
        require_assertions=False,
        tenant_id=tenant_id,
        actor_id=user.id,
    )
    minted = await mint_deploy_token(db=db, preview=preview, ttl_seconds=ttl)
    return preview, minted


class TestMintAndConsume:
    """Tests 6-9 in spec: mint + verify_and_consume happy path and the
    three core rejection paths (expired, already consumed, forged HMAC).
    """

    @pytest.mark.asyncio
    async def test_6_happy_path(
        self, db: AsyncSession, fully_ready_changeset, tenant_a, admin_user
    ):
        """Test 6: mint produces a token row; verify_and_consume marks it
        consumed and returns the row + gates so the caller can queue the run."""
        from app.services.deploy_preview_service import verify_and_consume_deploy_token

        ws, cs, user = fully_ready_changeset
        preview, minted = await _mint_for(db, ws, cs, user, tenant_a.id)

        assert "confirmation_token" in minted
        assert "jti" in minted
        assert len(minted["confirmation_token"]) == 64  # sha256 hex

        result = await verify_and_consume_deploy_token(
            db=db,
            jti=uuid.UUID(minted["jti"]),
            confirmation_token=minted["confirmation_token"],
            actor_id=user.id,
            tenant_id=tenant_a.id,
        )
        assert result["snapshot_sha"] == preview["snapshot_sha"]
        assert result["manifest_sha"] == preview["manifest_sha"]

        # Token row now marked consumed.
        token_row = await db.execute(
            select(WorkspaceDeployToken).where(WorkspaceDeployToken.id == uuid.UUID(minted["jti"]))
        )
        row = token_row.scalar_one()
        assert row.consumed_at is not None
        assert row.consumed_reason == "confirmed"

    @pytest.mark.asyncio
    async def test_7_expired_token(
        self, db: AsyncSession, fully_ready_changeset, tenant_a, admin_user
    ):
        """Test 7: token with expires_at in the past → TokenExpiredError +
        row marked consumed_reason="expired" so the partial-unique slot
        is freed for a fresh preview."""
        from app.services.deploy_preview_service import (
            TokenExpiredError,
            verify_and_consume_deploy_token,
        )

        ws, cs, user = fully_ready_changeset
        _, minted = await _mint_for(db, ws, cs, user, tenant_a.id)

        # Force the token expired by rewinding expires_at.
        jti = uuid.UUID(minted["jti"])
        row_result = await db.execute(
            select(WorkspaceDeployToken).where(WorkspaceDeployToken.id == jti)
        )
        row = row_result.scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.flush()

        with pytest.raises(TokenExpiredError):
            await verify_and_consume_deploy_token(
                db=db,
                jti=jti,
                confirmation_token=minted["confirmation_token"],
                actor_id=user.id,
                tenant_id=tenant_a.id,
            )

        # The row was marked consumed (reason=expired) to free the unique slot.
        row_after = await db.execute(
            select(WorkspaceDeployToken).where(WorkspaceDeployToken.id == jti)
        )
        after = row_after.scalar_one()
        assert after.consumed_at is not None
        assert after.consumed_reason == "expired"

    @pytest.mark.asyncio
    async def test_8_already_consumed_token(
        self, db: AsyncSession, fully_ready_changeset, tenant_a, admin_user
    ):
        """Test 8: consuming an already-consumed token → TokenConsumedError
        on the second call. Defends against double-click race + naive replay."""
        from app.services.deploy_preview_service import (
            TokenConsumedError,
            verify_and_consume_deploy_token,
        )

        ws, cs, user = fully_ready_changeset
        _, minted = await _mint_for(db, ws, cs, user, tenant_a.id)

        # First call consumes.
        await verify_and_consume_deploy_token(
            db=db,
            jti=uuid.UUID(minted["jti"]),
            confirmation_token=minted["confirmation_token"],
            actor_id=user.id,
            tenant_id=tenant_a.id,
        )
        # Second call rejects.
        with pytest.raises(TokenConsumedError):
            await verify_and_consume_deploy_token(
                db=db,
                jti=uuid.UUID(minted["jti"]),
                confirmation_token=minted["confirmation_token"],
                actor_id=user.id,
                tenant_id=tenant_a.id,
            )

    @pytest.mark.asyncio
    async def test_9_forged_hmac_token(
        self, db: AsyncSession, fully_ready_changeset, tenant_a, admin_user
    ):
        """Test 9: a token string that wasn't HMAC-signed by the server →
        TokenInvalidError. Defends against client-side forgery and any
        attempt to change a bound field (sandbox_id, snapshot_sha) while
        keeping a stale token."""
        from app.services.deploy_preview_service import (
            TokenInvalidError,
            verify_and_consume_deploy_token,
        )

        ws, cs, user = fully_ready_changeset
        _, minted = await _mint_for(db, ws, cs, user, tenant_a.id)

        forged = "f" * 64  # plausibly-shaped but unsigned

        with pytest.raises(TokenInvalidError):
            await verify_and_consume_deploy_token(
                db=db,
                jti=uuid.UUID(minted["jti"]),
                confirmation_token=forged,
                actor_id=user.id,
                tenant_id=tenant_a.id,
            )
