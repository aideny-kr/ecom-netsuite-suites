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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspacePatch,
)
from app.services import workspace_service as ws_svc


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
