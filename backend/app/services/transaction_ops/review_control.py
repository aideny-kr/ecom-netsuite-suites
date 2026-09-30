"""Human stop of a calendar review; immutable findings and daily work survive."""

from sqlalchemy import String, cast, exists, select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.services import audit_service
from app.services.transaction_ops import state_service as state


def stopped_clause(run):
    return exists(
        select(AuditEvent.id).where(
            AuditEvent.tenant_id == run.tenant_id,
            AuditEvent.action == "transaction_ops.review.stopped",
            AuditEvent.resource_type == "transaction_review",
            AuditEvent.payload["config_id"].astext == cast(run.config_id, String),
            AuditEvent.resource_id == run.params_json["review"]["id"].astext,
        )
    )


async def stopped(db, tenant_id, config_id, review_id):
    return (
        await db.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action == "transaction_ops.review.stopped",
                AuditEvent.resource_type == "transaction_review",
                AuditEvent.resource_id == str(review_id),
                AuditEvent.payload["config_id"].astext == str(config_id),
            )
            .limit(1)
        )
        is not None
    )


async def stop_review(db, tenant_id, run_id, *, actor):
    await state._human(db, tenant_id, actor, "recon.run")
    root = await state.get_run(db, tenant_id, run_id)
    span = root.params_json.get("review")
    if not span or root.origin not in {"manual", "chat"}:
        raise state.StateError("not_a_period_review", 422)
    await state.get_config(db, tenant_id, root.config_id, lock=True)
    if await stopped(db, tenant_id, root.config_id, span["id"]):
        await state._commit(db, tenant_id)
        return {"review_id": span["id"], "status": "stopped"}
    query = select(TransactionRun).where(
        TransactionRun.tenant_id == tenant_id,
        TransactionRun.config_id == root.config_id,
        TransactionRun.params_json["review"] == span,
        TransactionRun.origin.in_(("manual", "chat")),
        TransactionRun.status.in_(("pending", "running")),
    )
    ids = set(await db.scalars(query.with_only_columns(TransactionRun.id)))
    # A runner can hold its row before reading configuration. Do not wait in
    # reverse lock order: retry this short control request after its checkpoint.
    active = list(await db.scalars(query.with_for_update(skip_locked=True).execution_options(populate_existing=True)))
    if {r.id for r in active} != ids:
        await db.rollback()
        raise state.StateError("review_busy", 409)
    now = await state.run_clock(db)
    for run in active:
        run.progress_json = {**run.progress_json, "review_stopped_at": now.isoformat()}
        await state._finish_audited(db, tenant_id, run, "stall", now)
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="transaction_ops",
        action="transaction_ops.review.stopped",
        actor_id=actor.id,
        resource_type="transaction_review",
        resource_id=span["id"],
        payload={"config_id": str(root.config_id), "run_ids": [str(r.id) for r in active]},
    )
    await state._commit(db, tenant_id)
    return {"review_id": span["id"], "status": "stopped"}
