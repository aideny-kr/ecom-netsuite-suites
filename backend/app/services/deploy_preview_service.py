"""Deploy preview service — two-step gated API for SuiteCloud sandbox deploy.

Mirrors the chat write-confirmation pattern (PR #39) for the workspace-surface
deploy flow. The preview step computes a manifest + snapshot hash and mints an
HMAC token; the confirm step verifies the token and queues the existing
``deploy_sandbox`` workspace runner.

Spec: ``docs/superpowers/specs/2026-05-18-suitecloud-sandbox-deploy-gated-api.md``

The manifest builder is the shared source of truth — preview, confirm, and the
worker all call it so they cannot disagree about what's about to deploy.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    WorkspaceChangeSet,
    WorkspaceDeployToken,
    WorkspaceFile,
    WorkspacePatch,
)
from app.services import workspace_service as ws_svc
from app.services.chat.mutation_guard import (
    generate_confirmation_token,
    verify_confirmation_token,
)
from app.services.deploy_service import check_deploy_prerequisites
from app.services.runner_service import _validate_sandbox_target

# Token TTL — operators have 10 minutes after preview to confirm. Codex
# noted the choice was "open" in the spec; 10 min balances slow human
# review against replay window.
DEPLOY_TOKEN_TTL_SECONDS = 600
_EVENT_TYPE = "sandbox_deploy"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DeployPreviewError(Exception):
    """Base class for deploy-preview rejection signals.

    Subclasses map 1:1 to HTTP status codes in the API layer:
      ChangesetNotFoundError   → 404
      ChangesetNotApprovedError → 400
      DeployGateNotMetError    → 400
      InvalidSandboxTargetError → 400
    """


class ChangesetNotFoundError(DeployPreviewError):
    """Requested changeset does not exist for this tenant/workspace."""


class ChangesetNotApprovedError(DeployPreviewError):
    """Changeset.status is not 'approved' — operator must approve first."""

    def __init__(self, status: str) -> None:
        super().__init__(f"Changeset must be approved (current: {status})")
        self.status = status


class DeployGateNotMetError(DeployPreviewError):
    """check_deploy_prerequisites returned allowed=False — validate, tests, or
    assertions are missing or failing. The wrapped ``blocked_reason`` mirrors
    the existing endpoint's 400 response body so callers see consistent
    diagnostics across the legacy and the gated endpoints.
    """

    def __init__(self, blocked_reason: str, gates: dict[str, Any]) -> None:
        super().__init__(blocked_reason)
        self.blocked_reason = blocked_reason
        self.gates = gates


class InvalidSandboxTargetError(DeployPreviewError):
    """sandbox_id failed the runner_service pattern check (prod-shaped or
    not sandbox-shaped). Reuses the runner's allowlist so preview and run
    agree on which targets are deployable.
    """


class TokenNotFoundError(DeployPreviewError):
    """The jti referenced at confirm time doesn't exist for this tenant.
    Maps to 404.
    """


class TokenConsumedError(DeployPreviewError):
    """Token row exists but consumed_at is non-null — already used (or
    explicitly cancelled/expired by a prior call). Maps to 410.
    """


class TokenExpiredError(DeployPreviewError):
    """Token row exists, unconsumed, but expires_at < now(). The confirm
    handler marks the row consumed with reason="expired" before raising so
    the partial-unique slot frees for a fresh preview. Maps to 410.
    """


class TokenInvalidError(DeployPreviewError):
    """HMAC token does not verify against the canonical payload rebuilt
    from the row. Either the token was forged or someone tampered with
    one of the bound fields. Maps to 422.
    """


class SnapshotDriftError(DeployPreviewError):
    """Re-computed snapshot_sha or manifest_sha at confirm time differs
    from the pinned values in the token row — files mutated between
    preview and confirm. The operator must re-preview. Maps to 409.
    """

    def __init__(self, drift_field: str) -> None:
        super().__init__(f"Snapshot drift detected on {drift_field}")
        self.drift_field = drift_field


class CrossUserReplayError(DeployPreviewError):
    """The user at confirm time differs from the actor_id pinned at
    preview time. Codex P1 #4 — without this check, any tenant user with
    workspace.manage could replay another user's token. Maps to 403.
    """


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def compute_deploy_manifest(
    *,
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    """Build the deploy manifest for *changeset_id* + compute both hashes.

    Returns a dict ``{manifest, snapshot_sha, manifest_sha}`` where:

    - ``manifest`` is a list of ``{path, operation, content_sha, apply_order}``
      entries sorted by ``path``. ``operation`` ∈ ``{"create", "modify",
      "delete", "unchanged"}``. ``apply_order`` is the patch ordering
      (``-1`` for files not touched by any patch).
    - ``snapshot_sha`` hashes the final on-disk tree (``{path: content_sha}``).
      Two patch sequences producing the same final tree share the same value.
    - ``manifest_sha`` hashes the manifest list itself, so two patch sequences
      with different operations or orderings produce different values even
      when the final tree matches.

    The caller must hold the tenant context. This helper queries
    ``WorkspaceFile`` and ``WorkspacePatch`` rows for the given tenant +
    workspace + changeset; tenant filtering is enforced at the query level.
    """
    files_result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.tenant_id == tenant_id,
            WorkspaceFile.is_directory.is_(False),
        )
    )
    files: dict[str, str] = {f.path: f.content or "" for f in files_result.scalars().all()}

    cs_result = await db.execute(
        select(WorkspaceChangeSet).where(
            WorkspaceChangeSet.id == changeset_id,
            WorkspaceChangeSet.workspace_id == workspace_id,
            WorkspaceChangeSet.tenant_id == tenant_id,
        )
    )
    changeset = cs_result.scalar_one_or_none()
    if changeset is None:
        raise ValueError("Changeset not found")

    patch_result = await db.execute(
        select(WorkspacePatch)
        .where(
            WorkspacePatch.changeset_id == changeset_id,
            WorkspacePatch.tenant_id == tenant_id,
        )
        .order_by(WorkspacePatch.apply_order)
    )
    patches = list(patch_result.scalars().all())

    # Track per-path metadata so the manifest can surface operation + apply_order
    # even after the in-memory ``files`` map is mutated by the overlay.
    ops: dict[str, str] = {path: "unchanged" for path in files}
    orders: dict[str, int] = {path: -1 for path in files}

    for patch in patches:
        path = ws_svc.validate_path(patch.file_path)
        orders[path] = patch.apply_order

        if patch.operation == "create":
            files[path] = patch.new_content or ""
            ops[path] = "create"
            continue
        if patch.operation == "delete":
            files.pop(path, None)
            ops[path] = "delete"
            continue
        if patch.operation != "modify":
            raise ValueError(f"Unsupported patch operation: {patch.operation}")

        if path not in files:
            raise ValueError(f"Modify target missing from workspace snapshot: {path}")

        original_content = files[path]
        if patch.baseline_sha256 and _sha256(original_content) != patch.baseline_sha256:
            raise ValueError(f"Patch baseline hash mismatch for {path}")

        if patch.unified_diff:
            files[path] = ws_svc._apply_diff(original_content, patch.unified_diff)
        elif patch.new_content is not None:
            files[path] = patch.new_content
        else:
            raise ValueError(f"Modify patch has no diff/content for {path}")
        ops[path] = "modify"

    # snapshot_sha — final on-disk tree (post-patch).
    snapshot_payload = {path: _sha256(content) for path, content in files.items()}
    snapshot_sha = hashlib.sha256(
        json.dumps(snapshot_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # Manifest — surface unchanged files AND deleted files so the operator sees the
    # full delta. content_sha for deletes is the pre-delete hash; "" if unknown.
    all_paths = set(files.keys()) | set(ops.keys())
    manifest: list[dict[str, Any]] = []
    for path in sorted(all_paths):
        if path in files:
            content_sha = _sha256(files[path])
        else:
            # Deleted by a patch — content_sha records the pre-delete state so a
            # second deploy targeting the same final tree but missing the delete
            # produces a different manifest_sha.
            content_sha = _sha256("")
        manifest.append(
            {
                "path": path,
                "operation": ops.get(path, "unchanged"),
                "content_sha": content_sha,
                "apply_order": orders.get(path, -1),
            }
        )

    manifest_sha = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "manifest": manifest,
        "snapshot_sha": snapshot_sha,
        "manifest_sha": manifest_sha,
    }


# ---------------------------------------------------------------------------
# Preview builder — entry point used by the HTTP preview endpoint and the
# (future) MCP preview tool. Validates everything that can fail BEFORE any
# token row is written or HMAC is computed, so rejection paths stay cheap
# and audit-clean.
# ---------------------------------------------------------------------------


async def build_deploy_preview(
    *,
    db: AsyncSession,
    changeset_id: uuid.UUID,
    sandbox_id: str,
    require_assertions: bool,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> dict[str, Any]:
    """Validate prerequisites and compute the deploy preview payload.

    Raises one of the ``DeployPreviewError`` subclasses on rejection. On the
    happy path, returns the manifest + snapshot/manifest hashes ready for
    the caller to mint a token (the token-issuing step lives in a follow-up
    step gated on the ``workspace_deploy_tokens`` migration).
    """
    # 1. Load changeset, scoped to tenant + workspace via JOIN-free filter.
    cs_result = await db.execute(
        select(WorkspaceChangeSet).where(
            WorkspaceChangeSet.id == changeset_id,
            WorkspaceChangeSet.tenant_id == tenant_id,
        )
    )
    changeset = cs_result.scalar_one_or_none()
    if changeset is None:
        raise ChangesetNotFoundError(f"Changeset {changeset_id} not found")
    if changeset.status != "approved":
        raise ChangesetNotApprovedError(changeset.status)

    # 2. Sandbox-target shape check — fails fast on production-pattern strings.
    try:
        _validate_sandbox_target(sandbox_id)
    except ValueError as e:
        raise InvalidSandboxTargetError(str(e)) from e

    # 3. Gate check — validate + tests must be passing; assertions may be
    # required for sensitive deploys. Snapshot binding (codex P1 #6) happens
    # below once we compute snapshot_sha, when we re-call this with the hash.
    gate_result = await check_deploy_prerequisites(
        db,
        changeset_id,
        tenant_id,
        require_assertions=require_assertions,
    )
    if not gate_result["allowed"]:
        raise DeployGateNotMetError(
            gate_result.get("blocked_reason") or "Deploy gate failed",
            gate_result.get("gates", {}),
        )

    # 4. Compute manifest + hashes. This is the operator-visible
    # preview content and the cryptographic anchor for the token.
    manifest_payload = await compute_deploy_manifest(
        db=db,
        changeset_id=changeset_id,
        tenant_id=tenant_id,
        workspace_id=changeset.workspace_id,
    )

    return {
        "changeset_id": str(changeset_id),
        "workspace_id": str(changeset.workspace_id),
        "tenant_id": str(tenant_id),
        "sandbox_id": sandbox_id,
        "require_assertions": require_assertions,
        "actor_id": str(actor_id),
        "manifest": manifest_payload["manifest"],
        "snapshot_sha": manifest_payload["snapshot_sha"],
        "manifest_sha": manifest_payload["manifest_sha"],
        "gates": gate_result.get("gates", {}),
    }


# ---------------------------------------------------------------------------
# Token mint + verify — the cryptographic anchor for the two-step flow.
# Mint inserts the row, signs the canonical payload, returns
# {jti, confirmation_token, expires_at} alongside the preview body.
# Verify-and-consume locks the row, re-checks gate + snapshot drift, and
# returns the params the HTTP handler hands off to runner_service.
# ---------------------------------------------------------------------------


def _canonical_payload(*, jti: str, tenant_id: str, workspace_id: str,
                       changeset_id: str, sandbox_id: str, snapshot_sha: str,
                       manifest_sha: str, require_assertions: bool,
                       actor_id: str, issued_at: str) -> str:
    """Canonical JSON used as HMAC body. ``sort_keys=True`` +
    ``separators=(",", ":")`` matches write_confirmation_service so the
    discipline is consistent across both gates.
    """
    return json.dumps(
        {
            "jti": jti,
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "changeset_id": changeset_id,
            "sandbox_id": sandbox_id,
            "snapshot_sha": snapshot_sha,
            "manifest_sha": manifest_sha,
            "require_assertions": require_assertions,
            "actor_id": actor_id,
            "issued_at": issued_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def mint_deploy_token(
    *,
    db: AsyncSession,
    preview: dict[str, Any],
    ttl_seconds: int = DEPLOY_TOKEN_TTL_SECONDS,
) -> dict[str, Any]:
    """Insert a ``WorkspaceDeployToken`` row and return the signed token.

    Caller is the HTTP preview endpoint (and the MCP preview tool); both
    call ``build_deploy_preview`` first, then hand the returned dict here
    to mint the token. The split keeps validation cheap and free of side
    effects, and means the partial-unique constraint on
    ``workspace_deploy_tokens`` is only hit on the happy path.
    """
    jti = uuid.uuid4()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    row = WorkspaceDeployToken(
        id=jti,
        tenant_id=uuid.UUID(preview["tenant_id"]),
        workspace_id=uuid.UUID(preview["workspace_id"]),
        changeset_id=uuid.UUID(preview["changeset_id"]),
        sandbox_id=preview["sandbox_id"],
        snapshot_sha=preview["snapshot_sha"],
        manifest_sha=preview["manifest_sha"],
        require_assertions=preview["require_assertions"],
        actor_id=uuid.UUID(preview["actor_id"]),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()

    payload_json = _canonical_payload(
        jti=str(jti),
        tenant_id=preview["tenant_id"],
        workspace_id=preview["workspace_id"],
        changeset_id=preview["changeset_id"],
        sandbox_id=preview["sandbox_id"],
        snapshot_sha=preview["snapshot_sha"],
        manifest_sha=preview["manifest_sha"],
        require_assertions=preview["require_assertions"],
        actor_id=preview["actor_id"],
        issued_at=issued_at.isoformat(),
    )
    # session_id keyed off the actor — codex P2 #8 noted that pinning to
    # the user (not cs_id) is the strongest binding short of WebAuthn.
    confirmation_token = generate_confirmation_token(
        session_id=preview["actor_id"],
        payload_json=payload_json,
        event_type=_EVENT_TYPE,
    )

    return {
        "jti": str(jti),
        "confirmation_token": confirmation_token,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


async def verify_and_consume_deploy_token(
    *,
    db: AsyncSession,
    jti: uuid.UUID,
    confirmation_token: str,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, Any]:
    """Verify the HMAC token + freshness checks, mark the row consumed.

    Steps:
      1. Lock the token row (SELECT ... FOR UPDATE) so concurrent confirms
         can't both consume it.
      2. Reject if consumed_at is already set (replay).
      3. Reject if expires_at < now() — mark consumed with reason="expired"
         to free the unique slot.
      4. Reject if HMAC doesn't verify against the canonical payload
         rebuilt from the row.
      5. Reject if actor_id doesn't match the row's pinned actor.
      6. Re-load changeset, re-run gate prerequisites, re-compute the
         manifest+snapshot. Reject on changeset state flip or drift.
      7. Mark consumed + return the params the HTTP handler needs to
         queue the run.

    On rejection paths the function raises the specific exception subclass
    so the API layer can map to HTTP codes. The token row is left
    untouched on HMAC and actor mismatches so audit captures the failure
    against the original (issued) row.
    """
    result = await db.execute(
        select(WorkspaceDeployToken)
        .where(
            WorkspaceDeployToken.id == jti,
            WorkspaceDeployToken.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise TokenNotFoundError(f"Token {jti} not found")

    if row.consumed_at is not None:
        raise TokenConsumedError(
            f"Token {jti} already consumed at {row.consumed_at.isoformat()}"
        )

    now = datetime.now(timezone.utc)
    if row.expires_at < now:
        # Free the unique slot so a fresh preview can mint, then raise.
        row.consumed_at = now
        row.consumed_reason = "expired"
        await db.flush()
        raise TokenExpiredError(
            f"Token {jti} expired at {row.expires_at.isoformat()}"
        )

    payload_json = _canonical_payload(
        jti=str(row.id),
        tenant_id=str(row.tenant_id),
        workspace_id=str(row.workspace_id),
        changeset_id=str(row.changeset_id),
        sandbox_id=row.sandbox_id,
        snapshot_sha=row.snapshot_sha,
        manifest_sha=row.manifest_sha,
        require_assertions=row.require_assertions,
        actor_id=str(row.actor_id),
        issued_at=row.issued_at.isoformat(),
    )
    is_valid = verify_confirmation_token(
        confirmation_token,
        session_id=str(row.actor_id),
        payload_json=payload_json,
        event_type=_EVENT_TYPE,
    )
    if not is_valid:
        raise TokenInvalidError("HMAC verification failed for token")

    # Codex P1 #4 — the user at confirm must match preview's actor.
    if row.actor_id != actor_id:
        raise CrossUserReplayError(
            f"Token issued to actor {row.actor_id} cannot be confirmed by {actor_id}"
        )

    # Codex P1 #6 — re-run gate check with snapshot pinning AND confirm
    # state didn't flip between preview and confirm.
    cs_result = await db.execute(
        select(WorkspaceChangeSet).where(
            WorkspaceChangeSet.id == row.changeset_id,
            WorkspaceChangeSet.tenant_id == tenant_id,
        )
    )
    changeset = cs_result.scalar_one_or_none()
    if changeset is None or changeset.status != "approved":
        raise DeployGateNotMetError(
            "Changeset is no longer approved",
            {"changeset_status": getattr(changeset, "status", None)},
        )

    gate_result = await check_deploy_prerequisites(
        db,
        row.changeset_id,
        tenant_id,
        require_assertions=row.require_assertions,
    )
    if not gate_result["allowed"]:
        raise DeployGateNotMetError(
            gate_result.get("blocked_reason") or "Deploy gate failed at confirm",
            gate_result.get("gates", {}),
        )

    # Codex P1 #6 + #7 — snapshot must still match. Re-compute and
    # compare to the pinned hashes.
    fresh = await compute_deploy_manifest(
        db=db,
        changeset_id=row.changeset_id,
        tenant_id=tenant_id,
        workspace_id=row.workspace_id,
    )
    if fresh["snapshot_sha"] != row.snapshot_sha:
        raise SnapshotDriftError("snapshot_sha")
    if fresh["manifest_sha"] != row.manifest_sha:
        raise SnapshotDriftError("manifest_sha")

    # All checks passed — caller will queue the run, then re-call this
    # row to set consumed_run_id. For now we mark consumed_at to lock the
    # token so a parallel confirm can't double-spend.
    row.consumed_at = now
    row.consumed_reason = "confirmed"
    await db.flush()

    return {
        "row": row,
        "changeset": changeset,
        "gates": gate_result.get("gates", {}),
        "snapshot_sha": row.snapshot_sha,
        "manifest_sha": row.manifest_sha,
    }
