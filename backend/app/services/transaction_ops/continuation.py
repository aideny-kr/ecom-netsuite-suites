"""Finite continuations for productive investigations, using the existing queue/leases."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.models.user import User
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service
from app.services.transaction_ops.runner import enabled

MAX_PARTS = 16
MAX_CYCLE_AGE = timedelta(days=1)


async def continuation_result(db, tenant_id, run_id):
    child = await db.scalar(
        select(TransactionRun)
        .where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.progress_json["continuation_of"].astext == str(run_id),
        )
        .order_by(TransactionRun.created_at, TransactionRun.id)
        .limit(1)
    )
    blocked = await db.scalar(
        select(AuditEvent.payload)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.category == "transaction_ops",
            AuditEvent.action == "transaction_ops.run.continuation_blocked",
            AuditEvent.resource_type == TransactionRun.__tablename__,
            AuditEvent.resource_id == str(run_id),
        )
        .limit(1)
    )
    return child, blocked


def next_metadata(previous, now):
    progress = previous.progress_json or {}
    part = progress.get("continuation_part", 1)
    if type(part) is not int or not 1 <= part < MAX_PARTS:
        raise ValueError("part_limit")
    started = (
        datetime.fromisoformat(progress["continuation_started_at"])
        if progress.get("continuation_started_at")
        else previous.created_at
    )
    if started.utcoffset() is None or now - started >= MAX_CYCLE_AGE or started > now:
        raise ValueError("cycle_expired")
    baseline = progress.get("continuation_baseline") or {}
    counts = {key: progress.get(key, 0) for key in ("processed", "scan_count")}
    counts.update(
        {
            key: progress[key]
            for key in ("refund_scan_count", "outside_scope", "destination_scan_count")
            if key in progress
        }
    )
    if not any(counts[key] > baseline.get(key, 0) for key in counts) or progress.get("restart_scan"):
        raise ValueError("no_progress")
    return {
        "continuation_root_id": str(UUID(progress.get("continuation_root_id") or str(previous.id))),
        "continuation_part": part + 1,
        "continuation_started_at": started.isoformat(),
        "continuation_baseline": counts,
        "continuation_of": str(previous.id),
    }


async def continue_budget_run(db, tenant_id, run_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    previous = await state_service.get_run(db, tenant_id, run_id)
    config = await state_service.get_config(db, tenant_id, previous.config_id, lock=True)
    await db.refresh(previous)
    if previous.status != "finished" or previous.termination_reason != "budget" or previous.origin == "recovery":
        await state_service._commit(db, tenant_id)
        return None
    child, blocked = await continuation_result(db, tenant_id, run_id)
    if child is not None:
        await state_service._commit(db, tenant_id)
        return child
    if blocked:
        await state_service._commit(db, tenant_id)
        return None
    try:
        if not config.enabled or (previous.origin == "schedule" and not config.schedule_enabled):
            raise ValueError("paused")
        if not await enabled(db, tenant_id):
            raise ValueError("feature_unavailable")
        metadata = next_metadata(previous, now)
        actor = None
        if previous.origin != "schedule":
            actor = await db.scalar(select(User).where(User.tenant_id == tenant_id, User.id == previous.initiated_by))
            await state_service._human(db, tenant_id, actor, "recon.run")
        active = await db.scalar(
            select(TransactionRun.id)
            .where(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.config_id == config.id,
                TransactionRun.status.in_(("pending", "running")),
            )
            .limit(1)
        )
        if active:
            await state_service._commit(db, tenant_id)
            return None
        key = f"continue:{metadata['continuation_root_id']}:{metadata['continuation_part']}"
        request = RunCreate(**{**previous.params_json, "evaluation_key": key})
        child = await state_service.create_run(
            db,
            tenant_id,
            config.id,
            request,
            actor=actor,
            now=now,
            resume_from_run_id=previous.id,
            automatic_continuation=True,
        )
    except (ValueError, state_service.StateError) as exc:
        reason = exc.code if isinstance(exc, state_service.StateError) else str(exc)
        safe_reason = (
            reason
            if reason
            in {"paused", "part_limit", "cycle_expired", "no_progress", "permission_denied", "feature_unavailable"}
            else "continuation_unavailable"
        )
        await state_service._audit(db, tenant_id, "run.continuation_blocked", previous, payload={"reason": safe_reason})
        await state_service._commit(db, tenant_id)
        return None
    return child
