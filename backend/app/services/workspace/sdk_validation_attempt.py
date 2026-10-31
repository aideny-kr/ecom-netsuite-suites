"""Inactive durable adapter for the fixed SDK broker.

The worker routes explicitly selected runs here, but runtime/key configuration
remains disabled. The run row is
the exclusive journal lock; the committed claim survives worker loss. Existing
artifact storage supplies tenant RLS and run-cascade cleanup. These new artifact
types have no writer outside this module. Never reset a claimed run to retry it.
This receipt is deliberately distinct from deploy prerequisite result_json.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.tenant import Tenant
from app.models.user import Permission, RolePermission, User, UserRole
from app.models.workspace import Workspace, WorkspaceArtifact, WorkspaceChangeSet, WorkspaceRun
from app.services import audit_service
from app.services.workspace import credential_executor
from app.services.workspace.credential_contract import AuthSeederError
from app.services.workspace.sdk_validation_receipt import require_receipt_inputs, verify_validation_receipt
from app.services.workspace.target_identity import require_current_target
from app.services.workspace.trusted_sdf_project import PROJECT_FORMAT, build_ready_reviewed_project

ATTEMPT = "sdk_validation_attempt"
RECEIPT = "sdk_validation_receipt"
OUTCOME = "sdk_validation_outcome"
SELECTION = "sdk_validation_selection"
_ERROR = "SDK validation requires current authorized project and sandbox connection"


class ValidationAttemptError(AuthSeederError):
    """Sanitized journal/authority error; never an instruction to replay."""


@dataclass(frozen=True)
class ValidationRuntime:
    """Trusted operator input only; no settings or user-controlled activation."""

    image: str
    socket: str
    docker: str
    receipt_key: bytes = field(repr=False)
    verification_keys: tuple[bytes, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if (
            type(self.image) is not str
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image, re.ASCII)
            or type(self.receipt_key) is not bytes
            or not 32 <= len(self.receipt_key) <= 4096
            or type(self.socket) is not str
            or not Path(self.socket).is_absolute()
            or type(self.docker) is not str
            or not Path(self.docker).is_absolute()
            or type(self.verification_keys) is not tuple
            or len(self.verification_keys) > 7
            or any(type(key) is not bytes or not 32 <= len(key) <= 4096 for key in self.verification_keys)
        ):
            raise ValidationAttemptError("SDK validation requires reviewed runtime configuration")
        if len({hashlib.sha256(key).digest() for key in (self.receipt_key, *self.verification_keys)}) != (
            1 + len(self.verification_keys)
        ):
            raise ValidationAttemptError("SDK validation requires distinct receipt keys")

    @property
    def receipt_key_id(self) -> str:
        return hmac.new(self.receipt_key, b"suite-studio-sdk-validation-key-id-v1", hashlib.sha256).hexdigest()

    def verification_key(self, claim: dict) -> bytes:
        # Legacy claims did not pin a key. Preserve their original single-key
        # behavior; never guess a historical key by trying every signature.
        fingerprint = claim.get("receipt_key_id")
        if fingerprint is None and "receipt_key_id" not in claim:
            return self.receipt_key
        if type(fingerprint) is str and re.fullmatch(r"[a-f0-9]{64}", fingerprint, re.ASCII):
            for key in (self.receipt_key, *self.verification_keys):
                if hmac.new(key, b"suite-studio-sdk-validation-key-id-v1", hashlib.sha256).hexdigest() == fingerprint:
                    return key
        raise ValidationAttemptError("SDK validation receipt key is unavailable; reconcile without replay")


async def _run(db, tenant_id, run_id):
    run = await db.scalar(
        select(WorkspaceRun)
        .where(WorkspaceRun.id == run_id, WorkspaceRun.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.run_type != "suitecloud_validate" or run.changeset_id is None:
        raise ValidationAttemptError(_ERROR)
    return run


async def _state(db, run, connection_id):
    # Explicit membership tenant prevents a corrupt cross-company UserRole from
    # borrowing a global permission. Checks precede credential decryption.
    allowed = await db.scalar(
        select(User.id)
        .join(Tenant, Tenant.id == User.tenant_id)
        .join(UserRole, UserRole.user_id == User.id)
        .join(RolePermission, RolePermission.role_id == UserRole.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .join(Workspace, Workspace.tenant_id == Tenant.id)
        .join(WorkspaceChangeSet, WorkspaceChangeSet.workspace_id == Workspace.id)
        .where(
            User.id == run.triggered_by,
            User.tenant_id == run.tenant_id,
            UserRole.tenant_id == run.tenant_id,
            User.is_active.is_(True),
            Tenant.is_active.is_(True),
            Workspace.id == run.workspace_id,
            Workspace.status == "active",
            WorkspaceChangeSet.id == run.changeset_id,
            WorkspaceChangeSet.tenant_id == run.tenant_id,
            WorkspaceChangeSet.status.in_(["draft", "approved"]),
            Permission.codename == "workspace.manage",
        )
        .limit(1)
    )
    if allowed is None:
        raise ValidationAttemptError(_ERROR)
    # Local import avoids the existing preview/runner import cycle.
    from app.services.deploy_preview_service import compute_deploy_snapshot

    snapshot = await compute_deploy_snapshot(
        db=db, changeset_id=run.changeset_id, tenant_id=run.tenant_id, workspace_id=run.workspace_id
    )
    inputs = dict(
        files=snapshot["files"],
        manifest=snapshot["manifest"],
        expected_snapshot_sha=snapshot["snapshot_sha"],
        expected_manifest_sha=snapshot["manifest_sha"],
        project_format=PROJECT_FORMAT,
    )
    build_ready_reviewed_project(**inputs)
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.id == connection_id,
            Connection.tenant_id == run.tenant_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .execution_options(populate_existing=True)
    )
    try:
        if connection is None or len(connection.encrypted_credentials) > 64 * 1024:
            raise ValueError()
        credentials = decrypt_credentials(connection.encrypted_credentials)
        ci = credentials["suitecloud_ci"]
        private_key = ci["private_key"].encode("ascii")
        identity = credential_executor._identity(
            credentials["account_id"], ci["role_id"], ci["certificate_id"], private_key
        )
    except Exception:
        raise ValidationAttemptError(_ERROR) from None
    target = await require_current_target(
        db,
        run.tenant_id,
        dict(
            account_id=identity.account_id,
            role_id=identity.role_id,
            connection_id=str(connection.id),
            requested_certificate_id=identity.requested_certificate_id,
            local_public_key_sha256=identity.local_public_key_sha256,
        ),
    )
    context = {
        k: str(v)
        for k, v in dict(
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            changeset_id=run.changeset_id,
            run_id=run.id,
            actor_id=run.triggered_by,
            connection_id=connection.id,
        ).items()
    }
    return context, inputs, target, identity, private_key


def _content(payload):
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(content.encode("ascii")) > 4096:
        raise ValidationAttemptError("SDK validation journal exceeds limit")
    return content


def _add(db, run, kind, payload):
    content = _content(payload)
    db.add(
        WorkspaceArtifact(
            tenant_id=run.tenant_id,
            run_id=run.id,
            artifact_type=kind,
            content=content,
            size_bytes=len(content),
            sha256_hash=hashlib.sha256(content.encode("ascii")).hexdigest(),
        )
    )


async def _read(db, run, kind):
    rows = list(
        (
            await db.scalars(
                select(WorkspaceArtifact)
                .where(
                    WorkspaceArtifact.tenant_id == run.tenant_id,
                    WorkspaceArtifact.run_id == run.id,
                    WorkspaceArtifact.artifact_type == kind,
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    try:
        if len(rows) != 1:
            raise ValueError()
        row = rows[0]
        if type(row.content) is not str or len(row.content) > 4096:
            raise ValueError()
        content = row.content.encode("ascii")
        if row.size_bytes != len(content) or row.sha256_hash != hashlib.sha256(content).hexdigest():
            raise ValueError()
        return json.loads(row.content)
    except (TypeError, ValueError, UnicodeError):
        raise ValidationAttemptError("SDK validation journal is missing or inconsistent") from None


async def _commit(db, tenant_id):
    await db.commit()
    await set_tenant_context(db, str(tenant_id))


async def _has_selection(db, run):
    return (
        await db.scalar(
            select(WorkspaceArtifact.id)
            .where(
                WorkspaceArtifact.run_id == run.id,
                WorkspaceArtifact.tenant_id == run.tenant_id,
                WorkspaceArtifact.artifact_type == SELECTION,
            )
            .limit(1)
        )
        is not None
    )


async def bind_validation_connection(db: AsyncSession, *, tenant_id, run_id, connection_id) -> None:
    """Bind an explicit permitted target before queueing; caller must commit.

    Source is captured at the exclusive execution claim, as for other validation
    runs. This selection pins connection/account/role/requested key, not deployment
    authority. No arbitrary queue payload can create or replace this artifact.
    """
    run = await _run(db, tenant_id, run_id)
    existing = await db.scalar(
        select(WorkspaceArtifact.id)
        .where(WorkspaceArtifact.run_id == run_id, WorkspaceArtifact.tenant_id == tenant_id)
        .limit(1)
    )
    if run.status != "queued" or existing is not None:
        raise ValidationAttemptError("SDK validation selection already exists; create a new authorized run")
    try:
        context, _, target, _, _ = await _state(db, run, connection_id)
    except Exception:
        raise ValidationAttemptError(_ERROR) from None
    selection = dict(version=1, context=context, target=target)
    _add(db, run, SELECTION, selection)
    # Keep routing fail-closed even if the artifact is missing/corrupt later.
    run.validator_engine = "suitecloud_sdk_pinned"
    run.gate_status = "block"
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="workspace",
        action="sdk_validation.selected",
        actor_id=run.triggered_by,
        resource_type="workspace_run",
        resource_id=str(run_id),
        payload=selection,
    )
    await db.flush()


async def _selected(db, run):
    selection = await _read(db, run, SELECTION)
    try:
        if type(selection) is not dict or set(selection) != {"version", "context", "target"}:
            raise ValueError()
        if type(selection["version"]) is not int or selection["version"] != 1:
            raise ValueError()
        connection_id = uuid.UUID(selection["context"]["connection_id"])
        context, inputs, target, identity, private_key = await _state(db, run, connection_id)
        if selection != dict(version=1, context=context, target=target):
            raise ValueError()
    except Exception:
        raise ValidationAttemptError("SDK validation selection or current authority changed") from None
    return selection, context, inputs, target, identity, private_key


async def execute_validation_attempt(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    runtime: ValidationRuntime,
    connection_id: uuid.UUID | None = None,
) -> WorkspaceRun:
    """Exclusive fixed-operation execution; no caller-controlled expected state.

    A cancellation/crash leaves the durable running claim for reconciliation.
    Failures after claim also consume this run; retry requires a new authorized
    run. No result_json/deploy authority is created, even on verified success.
    Before a claim is committed, refusals leave the caller's transaction open.
    The caller must commit/rollback (or close its session) on every exception
    to release the run/connection locks, including duplicate-delivery refusal.
    """
    run = await _run(db, tenant_id, run_id)
    prior = await db.scalar(
        select(WorkspaceArtifact.id)
        .where(
            WorkspaceArtifact.tenant_id == tenant_id,
            WorkspaceArtifact.run_id == run_id,
            WorkspaceArtifact.artifact_type.in_([ATTEMPT, RECEIPT, OUTCOME]),
        )
        .limit(1)
    )
    if run.status != "queued" or prior is not None:
        raise ValidationAttemptError("SDK validation run already claimed; reconcile without replay")
    if connection_id is not None and (run.validator_engine == "suitecloud_sdk_pinned" or await _has_selection(db, run)):
        raise ValidationAttemptError("Selected SDK validation must use its durable connection binding")
    selection_sha = None
    try:
        if connection_id is None:
            selection, context, inputs, target, identity, private_key = await _selected(db, run)
            selection_sha = hashlib.sha256(_content(selection).encode("ascii")).hexdigest()
        else:
            context, inputs, target, identity, private_key = await _state(db, run, connection_id)
    except Exception:
        raise ValidationAttemptError(_ERROR) from None
    claim = dict(
        context=context,
        attempt_nonce=secrets.token_hex(32),
        image=runtime.image,
        target=target,
        source_snapshot_sha=inputs["expected_snapshot_sha"],
        source_manifest_sha=inputs["expected_manifest_sha"],
        project_format=PROJECT_FORMAT,
        receipt_key_id=runtime.receipt_key_id,
    )
    if selection_sha is not None:
        claim["selection_sha"] = selection_sha
    _add(db, run, ATTEMPT, claim)
    run.status = "running"
    run.gate_status = "block"
    run.started_at = datetime.now(timezone.utc)
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="workspace",
        action="sdk_validation.claimed",
        actor_id=run.triggered_by,
        resource_type="workspace_run",
        resource_id=str(run_id),
        payload=claim,
    )
    await _commit(db, tenant_id)  # No row/connection lock spans network work.
    try:
        # Detect changes during claim commit, before any external operation.
        await _verify_claim(db, run_id, tenant_id, runtime, expected=claim)
        await _commit(db, tenant_id)
        envelope = await credential_executor.validate_project(
            account_id=identity.account_id,
            role_id=identity.role_id,
            certificate_id=identity.requested_certificate_id,
            private_key=private_key,
            **inputs,
            context=context,
            attempt_nonce=claim["attempt_nonce"],
            receipt_key=runtime.receipt_key,
            image=runtime.image,
            socket=runtime.socket,
            docker=runtime.docker,
        )
        run, current, current_identity, current_inputs = await _verify_claim(
            db, run_id, tenant_id, runtime, expected=claim
        )
        verify_validation_receipt(
            envelope=envelope,
            identity=current_identity,
            inputs=current_inputs,
            context=current["context"],
            attempt_nonce=current["attempt_nonce"],
            image=runtime.image,
            key=runtime.receipt_key,
        )
        _add(db, run, RECEIPT, envelope)
        run.status = "passed"
        outcome = "verified"
    except Exception:
        # Never store arbitrary exception text, credentials or child output.
        await db.rollback()
        await set_tenant_context(db, str(tenant_id))
        run = await _run(db, tenant_id, run_id)
        if run.status == "running":
            run.status = "failed"
        run.has_errors = True
        outcome = "uncertain"
    run.gate_status = "block"
    run.validator_engine = "suitecloud_sdk_pinned"
    run.completed_at = datetime.now(timezone.utc)
    _add(db, run, OUTCOME, dict(state=outcome, requires_reconciliation=outcome != "verified"))
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="workspace",
        action="sdk_validation." + outcome,
        actor_id=run.triggered_by,
        resource_type="workspace_run",
        resource_id=str(run_id),
        payload=dict(state=outcome, deployment_authority=False),
    )
    await _commit(db, tenant_id)
    return run


async def _verify_claim(db, run_id, tenant_id, runtime, expected=None):
    run = await _run(db, tenant_id, run_id)
    claim = await _read(db, run, ATTEMPT)
    if run.status not in ({"running"} if expected is not None else {"running", "passed"}) or (
        expected is not None and claim != expected
    ):
        raise ValidationAttemptError("SDK validation attempt changed; reconcile without replay")
    try:
        fields = {
            "context",
            "attempt_nonce",
            "image",
            "target",
            "source_snapshot_sha",
            "source_manifest_sha",
            "project_format",
        }
        optional = {"selection_sha", "receipt_key_id"}
        if type(claim) is not dict or not fields <= set(claim) or set(claim) - fields - optional:
            raise ValueError()
        key = runtime.verification_key(claim)
        # A retained verification key never signs a new/in-flight attempt.
        if expected is not None and key != runtime.receipt_key:
            raise ValueError()
        require_receipt_inputs(context=claim["context"], attempt_nonce=claim["attempt_nonce"], key=key)
        if "selection_sha" in claim:
            selection, context, inputs, target, identity, _ = await _selected(db, run)
            if claim["selection_sha"] != hashlib.sha256(_content(selection).encode("ascii")).hexdigest():
                raise ValueError()
        else:
            if await _has_selection(db, run):
                raise ValueError()
            context, inputs, target, identity, _ = await _state(db, run, uuid.UUID(claim["context"]["connection_id"]))
    except Exception:
        raise ValidationAttemptError("SDK validation journal or current authority is inconsistent") from None
    if (
        claim["context"] != context
        or claim["target"] != target
        or claim["image"] != runtime.image
        or claim["source_snapshot_sha"] != inputs["expected_snapshot_sha"]
        or claim["source_manifest_sha"] != inputs["expected_manifest_sha"]
        or claim["project_format"] != PROJECT_FORMAT
    ):
        raise ValidationAttemptError("SDK validation authority or source changed; reconcile without replay")
    return run, claim, identity, inputs


async def read_validation_receipt(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, runtime: ValidationRuntime
) -> dict:
    """Reverify serialized evidence and current authority; no deployment grant.

    Caller owns this short transaction and must commit/rollback to release locks.
    No broker call or retry occurs, including for an uncertain/missing receipt.
    """
    run, claim, identity, inputs = await _verify_claim(db, run_id, tenant_id, runtime)
    outcome = await _read(db, run, OUTCOME)
    if run.status != "passed" or outcome != dict(state="verified", requires_reconciliation=False):
        raise ValidationAttemptError("SDK validation outcome requires reconciliation")
    return verify_validation_receipt(
        envelope=await _read(db, run, RECEIPT),
        identity=identity,
        inputs=inputs,
        context=claim["context"],
        attempt_nonce=claim["attempt_nonce"],
        image=runtime.image,
        key=runtime.verification_key(claim),
    )
