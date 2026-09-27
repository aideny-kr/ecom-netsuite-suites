"""Finite continuations for productive investigations, using the existing queue/leases."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.models.user import User
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service
from app.services.transaction_ops.runner import enabled

MAX_PARTS = 16
MAX_CYCLE_AGE = timedelta(days=1)
READ_RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))


def scheduled_read_stop(previous):
    """Only a recorded, unresolved transient READ can earn a delayed retry."""
    from app.services.transaction_ops.read_recovery import TRANSIENT_READ_CODES

    progress = getattr(previous, "progress_json", None) or {}
    failure = progress.get("last_read_failure") or {}
    return (
        getattr(previous, "origin", None) == "schedule"
        and getattr(previous, "status", None) == "finished"
        and getattr(previous, "termination_reason", None) == "budget"
        and progress.get("read_stop_reason") in {"retry_limit", "retry_deadline"}
        and isinstance(failure, dict)
        and failure.get("retryable") is True
        and failure.get("resolved") is False
        and failure.get("code") in TRANSIENT_READ_CODES
        and _read_stopped_this_run(previous, progress, failure)
    )


def _read_stopped_this_run(previous, progress, failure):
    owner = progress.get("read_stop_run_id")
    if owner is not None:
        return owner == str(previous.id)
    # Compatibility for pre-upgrade checkpoints: an inherited diagnostic is
    # not the stop cause of a later, productive invocation.
    try:
        observed = datetime.fromisoformat(failure["observed_at"])
        return previous.created_at <= observed <= previous.finished_at
    except (KeyError, TypeError, ValueError):
        return False


def read_retry_due(previous, now):
    if previous is None or not scheduled_read_stop(previous):
        return False
    try:
        next_metadata(previous, now)
    except (ValueError, TypeError):
        return False
    return True


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
        # Never let an older retryable no-progress refusal hide a later
        # permission/feature/operator stop when reconsidering a saved read.
        .order_by(
            (AuditEvent.payload["reason"].astext == "no_progress").asc().nullsfirst(),
            AuditEvent.timestamp.desc(),
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
            for key in ("refund_scan_count", "outside_scope", "destination_scan_count", "dependency_step_count")
            if key in progress
        }
    )
    retry = scheduled_read_stop(previous)
    retry_count = progress.get("continuation_read_retry_count", 0)
    if type(retry_count) is not int or not 0 <= retry_count <= len(READ_RETRY_DELAYS):
        raise ValueError("read_retry_limit")
    if progress.get("restart_scan"):
        raise ValueError("no_progress")
    if retry:
        if retry_count == len(READ_RETRY_DELAYS):
            raise ValueError("read_retry_limit")
        finished = getattr(previous, "finished_at", None)
        if finished is None or finished.utcoffset() is None or finished > now:
            raise ValueError("no_progress")
        if now < finished + READ_RETRY_DELAYS[retry_count]:
            raise ValueError("read_retry_wait")
        retry_count += 1
    elif not any(counts[key] > baseline.get(key, 0) for key in counts):
        raise ValueError("no_progress")
    return {
        "continuation_root_id": str(UUID(progress.get("continuation_root_id") or str(previous.id))),
        "continuation_part": part + 1,
        "continuation_started_at": started.isoformat(),
        "continuation_baseline": counts,
        "continuation_of": str(previous.id),
        # The ordinary part/age caps still apply; this count never resets in a
        # continuation, even if intervening runs made progress. A new daily
        # cycle clears continuation metadata through the existing create path.
        "continuation_read_retry_count": retry_count,
    }


async def continue_budget_run(db, tenant_id, run_id, *, now=None):
    now = await state_service.run_clock(db, now)
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
    # A pre-release no-progress refusal may have stranded a transient read.
    # Reconsider only that reason, after backoff and under the same finite caps.
    if blocked and not (blocked.get("reason") == "no_progress" and read_retry_due(previous, now)):
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
                # Daily reads and manual reviews have independent finite cycles.
                # Recovery work keeps its existing serialization semantics.
                or_(TransactionRun.origin == "schedule", TransactionRun.origin == "recovery")
                if previous.origin == "schedule"
                else TransactionRun.origin != "schedule",
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
        if reason == "read_retry_wait":
            # The scheduler owns the wake-up; no sleeping worker or permanent
            # blocked audit is needed while the deterministic backoff elapses.
            await state_service._commit(db, tenant_id)
            return None
        safe_reason = (
            reason
            if reason
            in {
                "paused",
                "part_limit",
                "cycle_expired",
                "no_progress",
                "permission_denied",
                "feature_unavailable",
                "read_retry_limit",
            }
            else "continuation_unavailable"
        )
        await state_service._audit(db, tenant_id, "run.continuation_blocked", previous, payload={"reason": safe_reason})
        await state_service._commit(db, tenant_id)
        return None
    return child
