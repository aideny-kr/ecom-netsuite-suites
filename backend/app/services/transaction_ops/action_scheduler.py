"""Bounded durable dispatch of approved intents and read-only outcome recovery."""

import asyncio
from datetime import timezone

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionConfig as Config
from app.models.transaction_ops import TransactionOperation as Operation
from app.models.transaction_ops import TransactionProposal as Proposal
from app.models.transaction_ops import TransactionRun as Run
from app.services import feature_flag_service
from app.services.transaction_ops.scheduler import _BROKER_IO_TIMEOUT, _DISPATCH_TIMEOUT
from app.workers.celery_app import celery_app

_LIMIT = 200
_TASKS = {
    "execute": "tasks.transaction_ops_execute",
    "recover": "tasks.transaction_ops_recover",
    "credit_recover": "tasks.transaction_ops_recover_credit",
}


def publish_action(tenant_id, kind, identifier, *, app=celery_app):
    task = _TASKS[kind]
    key = {"execute": "proposal_id", "recover": "operation_id", "credit_recover": "message_id"}[kind]
    with app.connection_for_write(
        connect_timeout=_BROKER_IO_TIMEOUT,
        transport_options={
            "socket_connect_timeout": _BROKER_IO_TIMEOUT,
            "socket_timeout": _BROKER_IO_TIMEOUT,
            "retry_on_timeout": False,
            "max_retries": 0,
        },
    ) as connection:
        app.send_task(
            task,
            kwargs={"tenant_id": str(tenant_id), key: str(identifier)},
            queue="recon",
            retry=False,
            retry_policy={"max_retries": 0},
            connection=connection,
            ignore_result=True,
        )


async def _dispatch(tenant_id, kind, identifier, stats):
    try:
        await asyncio.wait_for(
            asyncio.to_thread(publish_action, tenant_id, kind, identifier), timeout=_DISPATCH_TIMEOUT
        )
        stats["dispatched"] += 1
    except Exception:
        stats["dispatch_failed"] += 1


async def _candidates(db, tenant_id, now):
    await set_tenant_context(db, str(tenant_id))
    attempted = exists(
        select(Operation.id).where(Operation.tenant_id == tenant_id, Operation.work_key == Proposal.work_key)
    )
    related = aliased(Proposal)
    unsettled = exists(
        select(Operation.id)
        .join(related, and_(related.id == Operation.proposal_id, related.tenant_id == tenant_id))
        .where(
            Operation.tenant_id == tenant_id,
            Operation.status.in_(("executing", "unknown")),
            func.lower(func.replace(related.netsuite_account_id, "_", "-"))
            == func.lower(func.replace(Proposal.netsuite_account_id, "_", "-")),
            related.subsidiary_id == Proposal.subsidiary_id,
            related.record_type == Proposal.record_type,
            related.order_reference == Proposal.order_reference,
        )
    )
    executions = (
        (
            await db.execute(
                select(Proposal.id)
                .join(Config, and_(Config.id == Proposal.config_id, Config.tenant_id == tenant_id))
                .where(
                    Proposal.tenant_id == tenant_id,
                    Proposal.status == "approved",
                    Config.enabled.is_(True),
                    ~attempted,
                    ~unsettled,
                )
                .order_by(Proposal.decided_at, Proposal.id)
                .limit(_LIMIT + 1)
            )
        )
        .scalars()
        .all()
    )
    recovery_busy_or_done = exists(
        select(Run.id).where(
            Run.tenant_id == tenant_id,
            Run.origin == "recovery",
            Run.params_json["operation_id"].astext == cast(Operation.id, String),
            or_(Run.status == "finished", and_(Run.status == "running", Run.lease_until > now, Run.deadline_at > now)),
        )
    )
    recoveries = (
        (
            await db.execute(
                select(Operation.id)
                .join(Proposal, and_(Proposal.id == Operation.proposal_id, Proposal.tenant_id == tenant_id))
                .join(Config, and_(Config.id == Proposal.config_id, Config.tenant_id == tenant_id))
                .where(
                    Operation.tenant_id == tenant_id,
                    Config.enabled.is_(True),
                    ~recovery_busy_or_done,
                    or_(
                        and_(Operation.status == "executing", Operation.deadline_at <= now),
                        Operation.status == "unknown",
                    ),
                )
                .order_by(Operation.attempted_at, Operation.id)
                .limit(_LIMIT + 1)
            )
        )
        .scalars()
        .all()
    )
    return executions, recoveries


async def collect_due_actions(db, now):
    if now.utcoffset() is None:
        raise ValueError("An aware clock is required")
    now = now.astimezone(timezone.utc)
    stats = {
        "executions": 0,
        "recoveries": 0,
        "credit_recoveries": 0,
        "dispatched": 0,
        "dispatch_failed": 0,
        "tenant_failed": 0,
        "truncated": False,
        "termination_reason": "done",
    }
    try:
        async with asyncio.timeout(40):
            tenants = await feature_flag_service.list_tenants_with_flags(db, ("celigo", "reconciliation"))
            if tenants:
                offset = int(now.timestamp()) // 60 % len(tenants)
                tenants = tenants[offset:] + tenants[:offset]
            for tenant_id in tenants:
                try:
                    executions, recoveries = await _candidates(db, tenant_id, now)
                    from app.services.transaction_ops.accounting_recovery import candidates as credit_candidates

                    credits = await credit_candidates(db, tenant_id, now, limit=_LIMIT + 1)
                    await db.commit()
                    for kind, candidates, counter in (
                        ("credit_recover", credits, "credit_recoveries"),
                        ("recover", recoveries, "recoveries"),
                        ("execute", executions, "executions"),
                    ):
                        stats["truncated"] |= len(candidates) > _LIMIT
                        for identifier in candidates[:_LIMIT]:
                            stats[counter] += 1
                            await _dispatch(tenant_id, kind, identifier, stats)
                except Exception:
                    await db.rollback()
                    stats["tenant_failed"] += 1
    except TimeoutError:
        await db.rollback()
        stats["truncated"] = True
    if stats["truncated"]:
        stats["termination_reason"] = "budget"
    elif stats["dispatch_failed"] or stats["tenant_failed"]:
        stats["termination_reason"] = "error"
    return stats
