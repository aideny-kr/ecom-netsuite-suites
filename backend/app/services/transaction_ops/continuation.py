"""Finite continuations for productive investigations, using the existing queue/leases."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.models.user import User
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service
from app.services.transaction_ops.auth_recovery import auth_resume_candidate, auth_resume_ready, auth_stop
from app.services.transaction_ops.collection_recovery import collection_stop
from app.services.transaction_ops.runner import enabled

MAX_PARTS = 16
# Scheduled reads can use a full day of 15-minute work segments. Keep a fixed
# count as a spend bound even when API/order budgets make segments shorter.
SCHEDULE_MAX_PARTS = 96
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


def auth_resume_due(previous, now):
    """Wake recoverable auth checkpoints without trapping new pre-HTTP failures.

    Native 401 retains its established hard-stop contract. Pre-HTTP failures
    historically earned a new bounded daily run at the next cutoff; when their
    immediate continuation is exhausted, preserve that fallback, not a new
    permanent schedule stop. No continuation allowance is reset here.
    """
    if not auth_stop(previous):
        return False
    if previous.progress_json["last_read_failure"]["code"] == "netsuite_upstream_http_401":
        return True
    try:
        next_metadata(previous, now)
    except (ValueError, TypeError) as exc:
        return str(exc) not in {"cycle_expired", "part_limit", "auth_retry_limit"}
    return True


def scheduled_part_resume_candidate(previous, now):
    """Reconsider the former scheduled cap; creation still checks all blocks."""
    if (
        previous is None
        or getattr(previous, "origin", None) != "schedule"
        or getattr(previous, "status", None) != "finished"
        or getattr(previous, "termination_reason", None) != "budget"
        or (getattr(previous, "progress_json", None) or {}).get("continuation_part") != MAX_PARTS
    ):
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
        # No recoverable historical refusal may hide a hard stop, regardless
        # of which audit was recorded first.
        .order_by(
            AuditEvent.payload["reason"].astext.in_(("no_progress", "part_limit")).asc().nullsfirst(),
            AuditEvent.timestamp.desc(),
        )
        .limit(1)
    )
    return child, blocked


def next_metadata(previous, now, *, operator_retry=False):
    progress = previous.progress_json or {}
    part = progress.get("continuation_part", 1)
    limit = SCHEDULE_MAX_PARTS if getattr(previous, "origin", None) == "schedule" else MAX_PARTS
    if type(part) is not int or not 1 <= part < limit:
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
    collection_retry = collection_stop(previous, operator_retry=operator_retry)
    if collection_retry and (progress.get("last_collection_failure") or {}).get("code") == "collection_permanent":
        raise ValueError("collection_failure_permanent")
    retry = scheduled_read_stop(previous) or collection_retry
    diagnostic_count = progress.get("collection_diagnostic_retry_count", 0)
    if type(diagnostic_count) is not int or not 0 <= diagnostic_count <= 1:
        raise ValueError("collection_diagnostic_retry_limit")
    unknown = (
        collection_retry
        and (progress.get("last_collection_failure") or {}).get("code", "collection_unexpected")
        == "collection_unexpected"
    )
    if unknown and diagnostic_count == 1:
        raise ValueError("collection_diagnostic_retry_limit")
    auth_retry = auth_resume_candidate(previous)
    if auth_stop(previous) and not auth_retry:
        raise ValueError("auth_retry_limit")
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
    elif not auth_retry and not any(counts[key] > baseline.get(key, 0) for key in counts):
        raise ValueError("no_progress")
    metadata = {
        "continuation_root_id": str(UUID(progress.get("continuation_root_id") or str(previous.id))),
        "continuation_part": part + 1,
        "continuation_started_at": started.isoformat(),
        "continuation_baseline": counts,
        "continuation_of": str(previous.id),
        # The ordinary part/age caps still apply; this count never resets in a
        # continuation, even if intervening runs made progress. A new daily
        # cycle clears continuation metadata through the existing create path.
        "continuation_read_retry_count": retry_count,
        "collection_diagnostic_retry_count": diagnostic_count + int(unknown),
    }
    if auth_retry:
        metadata["auth_resume_count"] = 1
    return metadata


async def continue_budget_run(db, tenant_id, run_id, *, now=None, operator_retry=False, actor=None):
    now = await state_service.run_clock(db, now)
    previous = await state_service.get_run(db, tenant_id, run_id)
    config = await state_service.get_config(db, tenant_id, previous.config_id, lock=True)
    await db.refresh(previous)
    if previous.params_json.get("review"):
        from app.services.transaction_ops.review_control import stopped

        if await stopped(db, tenant_id, previous.config_id, previous.params_json["review"]["id"]):
            await state_service._commit(db, tenant_id)
            return None
    auth_retry = auth_stop(previous)
    collection_retry = collection_stop(previous, operator_retry=operator_retry)
    if operator_retry:
        await state_service._human(db, tenant_id, actor, "recon.run")
    if (
        previous.status != "finished"
        or (previous.termination_reason != "budget" and not auth_retry and not collection_retry)
        or previous.origin == "recovery"
    ):
        await state_service._commit(db, tenant_id)
        return None
    child, blocked = await continuation_result(db, tenant_id, run_id)
    if child is not None:
        await state_service._commit(db, tenant_id)
        return child
    if previous.origin == "schedule":
        # A new daily cycle resumes the checkpoint without continuation_of.
        # Once that newer run exists, replaying this parent must never fork
        # another chain from its older cursor. The config lock serializes both.
        latest = await db.scalar(
            select(TransactionRun.id)
            .where(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.config_id == config.id,
                TransactionRun.origin == "schedule",
            )
            .order_by(
                TransactionRun.created_at.desc(),
                TransactionRun.params_json["evaluation_key"].astext.desc(),
                TransactionRun.id.desc(),
            )
            .limit(1)
        )
        if latest != previous.id:
            await state_service._commit(db, tenant_id)
            return None
    # Reconsider only the old part cap or a retryable no-progress refusal.
    # Both paths recheck productivity/backoff and the current finite limits.
    if blocked and not (
        (blocked.get("reason") == "no_progress" and read_retry_due(previous, now))
        or (blocked.get("reason") == "part_limit" and scheduled_part_resume_candidate(previous, now))
    ):
        await state_service._commit(db, tenant_id)
        return None
    try:
        if not config.enabled or (previous.origin == "schedule" and not config.schedule_enabled):
            raise ValueError("paused")
        if not await enabled(db, tenant_id):
            raise ValueError("feature_unavailable")
        metadata = next_metadata(previous, now, operator_retry=operator_retry)
        if auth_retry and not await auth_resume_ready(db, tenant_id, previous, config, now):
            # Credentials can recover later. Keep the checkpoint, without a
            # permanent audit block or a provider request on each Beat tick.
            await state_service._commit(db, tenant_id)
            return None
        if not operator_retry:
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
                # Pending reviews may be waiting for this very collector. Its
                # continuation must exist before overlap arbitration can run.
                (TransactionRun.status == "running") | TransactionRun.params_json["review"].astext.is_(None)
                if previous.params_json.get("review")
                else True,
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
            automatic_auth_recovery=auth_retry,
            automatic_collection_recovery=collection_retry,
            operator_collection_retry=operator_retry,
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
                "auth_retry_limit",
                "collection_diagnostic_retry_limit",
                "collection_failure_permanent",
                "collection_scope_changed",
            }
            else "continuation_unavailable"
        )
        await state_service._audit(db, tenant_id, "run.continuation_blocked", previous, payload={"reason": safe_reason})
        await state_service._commit(db, tenant_id)
        return None
    return child
