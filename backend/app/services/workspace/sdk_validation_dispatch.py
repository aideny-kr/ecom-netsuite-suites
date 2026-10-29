"""Worker-only routing from durable selection; activation remains closed."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models.workspace import WorkspaceArtifact, WorkspaceRun
from app.services import audit_service, runner_service
from app.services.workspace import sdk_validation_attempt as attempts


def validation_runtime() -> attempts.ValidationRuntime | None:
    """No runtime is configured until custody, egress and live gates are met.

    Never accept this value from Celery kwargs, HTTP bodies or project files.
    Tests inject a synthetic trusted runtime at this server-only seam.
    """
    return None


async def execute_workspace_run(db, *, tenant_id, run_id, extra_params=None):
    run = await db.scalar(
        select(WorkspaceRun)
        .where(WorkspaceRun.id == run_id, WorkspaceRun.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise attempts.ValidationAttemptError("Workspace run is unavailable")
    selected = None
    if run.run_type == "suitecloud_validate":
        selected = await db.scalar(
            select(WorkspaceArtifact.id)
            .where(
                WorkspaceArtifact.tenant_id == tenant_id,
                WorkspaceArtifact.run_id == run_id,
                WorkspaceArtifact.artifact_type.in_(
                    [attempts.SELECTION, attempts.ATTEMPT, attempts.RECEIPT, attempts.OUTCOME]
                ),
            )
            .limit(1)
        )
    sdk_selected = run.run_type == "suitecloud_validate" and (
        run.validator_engine == "suitecloud_sdk_pinned" or selected is not None
    )
    if not sdk_selected:
        return await runner_service.execute_run(db, run_id, tenant_id, extra_params=extra_params)
    # A crash/cancellation/duplicate must never choose another executor or replay.
    if run.status != "queued":
        return run
    runtime = validation_runtime()
    reason = "SDK validation runtime is not enabled; reviewed custody, egress and sandbox acceptance are required"
    if runtime is not None:
        try:
            return await attempts.execute_validation_attempt(db, tenant_id=tenant_id, run_id=run_id, runtime=runtime)
        except attempts.ValidationAttemptError:
            # Only pre-claim refusal reaches here. Post-claim errors are consumed
            # by the journal; cancellation propagates and leaves its durable claim.
            reason = "SDK validation selection or current authority is invalid; create a new authorized run"
    run.status = "failed"
    run.gate_status = "block"
    run.has_errors = True
    run.validator_engine = "suitecloud_sdk_pinned"
    run.exit_code = -1
    run.completed_at = datetime.now(timezone.utc)
    await runner_service._store_artifact(db, run, "stderr", reason)
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="workspace",
        action="sdk_validation.dispatch_blocked",
        actor_id=run.triggered_by,
        resource_type="workspace_run",
        resource_id=str(run_id),
        payload=dict(reason=reason, deployment_authority=False),
    )
    await db.flush()
    return run
