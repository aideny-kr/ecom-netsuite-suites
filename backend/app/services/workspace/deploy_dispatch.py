"""Post-commit delivery of an already confirmed deployment.

The committed queued run is the durable pending record. Broker failures can be
retried explicitly without reconfirming or creating another external operation.
Only queued runs are deliverable; the worker owns the exclusive execution claim.
"""

import uuid

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.workspace import WorkspaceDeployToken, WorkspaceRun
from app.services import audit_service
from app.services.workspace.trusted_sdf_project import PROJECT_FORMAT


async def dispatch_confirmed_deploy(db, *, run_id: uuid.UUID, tenant_id: uuid.UUID, actor_id: uuid.UUID):
    # HTTP confirm may already have committed, clearing its SET LOCAL context.
    await set_tenant_context(db, str(tenant_id))
    result = await db.execute(
        select(WorkspaceRun, WorkspaceDeployToken)
        .join(WorkspaceDeployToken, WorkspaceDeployToken.consumed_run_id == WorkspaceRun.id)
        .where(
            WorkspaceRun.id == run_id,
            WorkspaceRun.tenant_id == tenant_id,
            WorkspaceRun.run_type == "deploy_sandbox",
            WorkspaceRun.triggered_by == actor_id,
            WorkspaceDeployToken.tenant_id == tenant_id,
            WorkspaceDeployToken.actor_id == actor_id,
            WorkspaceDeployToken.consumed_reason == "confirmed",
            WorkspaceDeployToken.consumed_at.is_not(None),
        )
    )
    matched = result.one_or_none()
    if matched is None:
        raise ValueError("Confirmed deployment not found")
    run, token = matched
    if run.status != "queued":
        return run, False
    if token.project_format != PROJECT_FORMAT:
        raise ValueError("Confirmed deployment has no supported bound project format; preview again")
    from app.services.deploy_preview_service import (
        DeployPreviewError,
        compute_deploy_snapshot,
        require_project_readiness,
    )

    if token.workspace_id != run.workspace_id or token.changeset_id != run.changeset_id:
        raise ValueError("Confirmed deployment project differs from the queued run")
    try:
        snapshot = await compute_deploy_snapshot(
            db=db, changeset_id=run.changeset_id, tenant_id=tenant_id, workspace_id=run.workspace_id
        )
        if snapshot["snapshot_sha"] != token.snapshot_sha or snapshot["manifest_sha"] != token.manifest_sha:
            raise ValueError("Confirmed deployment project changed; preview again")
        require_project_readiness(snapshot, {})
    except (DeployPreviewError, ValueError) as exc:
        raise ValueError("Confirmed deployment project is not ready; preview again") from exc
    params = {
        "sandbox_id": token.sandbox_id,
        "expected_snapshot_sha": token.snapshot_sha,
        "expected_manifest_sha": token.manifest_sha,
        "require_assertions": token.require_assertions,
        "expected_target_identity": token.target_identity,
        "expected_project_format": token.project_format,
    }
    correlation_id = run.correlation_id
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="workspace",
        action="deploy.dispatch_requested",
        actor_id=actor_id,
        resource_type="workspace_run",
        resource_id=str(run_id),
        correlation_id=correlation_id,
        payload={
            "delivery": "same_confirmed_queued_run",
            "target_identity": token.target_identity,
            "project_format": token.project_format,
        },
    )
    # Never publish a run that another database session cannot yet see.
    await db.commit()
    from app.workers.tasks.workspace_run import workspace_run_task

    try:
        workspace_run_task.delay(
            tenant_id=str(tenant_id), run_id=str(run_id), correlation_id=correlation_id, extra_params=params
        )
    except Exception:
        await set_tenant_context(db, str(tenant_id))
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="workspace",
            action="deploy.dispatch_pending",
            actor_id=actor_id,
            resource_type="workspace_run",
            resource_id=str(run_id),
            correlation_id=correlation_id,
            status="error",
            payload={"reason": "broker_unavailable", "retry": "deliver_same_queued_run"},
        )
        await db.commit()
        return run, True
    return run, False
