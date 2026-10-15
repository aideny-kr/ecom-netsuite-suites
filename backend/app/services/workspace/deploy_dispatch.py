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
    params = {
        "sandbox_id": token.sandbox_id,
        "expected_snapshot_sha": token.snapshot_sha,
        "expected_manifest_sha": token.manifest_sha,
        "require_assertions": token.require_assertions,
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
        payload={"delivery": "same_confirmed_queued_run"},
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
