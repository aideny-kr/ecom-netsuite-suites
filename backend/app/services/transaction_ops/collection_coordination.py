"""One collector for intersecting calendar windows; evidence reuse stays separate.

Called under the configuration row lock before a run acquires its lease. Waiting
does not consume provider budget or renew a run's finite queue/cycle lifetime.
"""

from datetime import datetime, timedelta

from sqlalchemy import DateTime, String, cast, exists, or_, select
from sqlalchemy.orm import aliased

from app.models.transaction_ops import TransactionRun


def calendar_collection(run):
    params = run.params_json or {}
    return (
        run.origin in {"manual", "chat", "schedule"}
        and (run.origin == "schedule" or bool(params.get("review")))
        and bool(params.get("window_start"))
        and bool(params.get("window_end"))
        and not params.get("order_references")
        and not params.get("operation_id")
    )


def _priority(run):
    started = (run.progress_json or {}).get("continuation_started_at")
    try:
        started = datetime.fromisoformat(started) if started else run.created_at
        if started.utcoffset() is None:
            started = run.created_at
    except (TypeError, ValueError):
        started = run.created_at
    root = (run.progress_json or {}).get("continuation_root_id") or str(run.id)
    return started, root


async def collection_blocker(db, tenant_id, run, now):
    """Return an earlier owner, without probing a provider or changing its state."""
    if not calendar_collection(run):
        return None
    from app.services.transaction_ops import state_service as state
    from app.services.transaction_ops.auth_recovery import auth_resume_candidate
    from app.services.transaction_ops.continuation import continuation_result, next_metadata

    r, child = TransactionRun, aliased(TransactionRun)
    params = run.params_json
    has_child = exists(
        select(child.id).where(
            child.tenant_id == tenant_id,
            child.config_id == run.config_id,
            child.progress_json["continuation_of"].astext == cast(r.id, String),
        )
    )
    newer_schedule = exists(
        select(child.id).where(
            child.tenant_id == tenant_id,
            child.config_id == run.config_id,
            child.origin == "schedule",
            child.created_at > r.created_at,
        )
    )
    candidates = list(
        (
            await db.scalars(
                select(r)
                .where(
                    r.tenant_id == tenant_id,
                    r.config_id == run.config_id,
                    r.id != run.id,
                    or_(
                        r.status.in_(("pending", "running")),
                        (r.status == "finished")
                        & r.termination_reason.in_(("budget", "error"))
                        & (r.finished_at > now - timedelta(days=1))
                        & ~has_child
                        & ((r.origin != "schedule") | ~newer_schedule),
                    ),
                    cast(r.params_json["window_start"].astext, DateTime(timezone=True))
                    < datetime.fromisoformat(params["window_end"]),
                    cast(r.params_json["window_end"].astext, DateTime(timezone=True))
                    > datetime.fromisoformat(params["window_start"]),
                )
                .order_by(r.created_at, r.id)
                .limit(513)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(candidates) > 512:
        raise state.StateError("collection_queue_limit")
    eligible = []
    for other in candidates:
        if not calendar_collection(other):
            continue
        if (other.params_json or {}).get("window_basis", "updated_at") != params.get("window_basis", "updated_at"):
            continue
        if other.status == "finished":
            if other.termination_reason != "budget" and not auth_resume_candidate(other):
                continue
            _, blocked = await continuation_result(db, tenant_id, other.id)
            if blocked and blocked.get("reason") not in {"no_progress", "part_limit"}:
                continue
            try:
                next_metadata(other, now)
            except (TypeError, ValueError) as exc:
                if str(exc) != "read_retry_wait":
                    continue
        elif other.status == "pending":
            deadline = state._first_claim_deadline(other, now)
            if deadline is None or deadline <= now:
                continue
        elif other.deadline_at <= now:
            # Its worker's evidence lease is no longer valid. Normal recovery
            # persists budget termination; this does not extend its lifetime.
            continue
        # A running owner retains priority even if an older request arrives.
        # An expired lease remains the owner's recoverable checkpoint, rather
        # than permission for a second lineage to repeat that collection.
        if run.status == "running":
            owns_before = other.status == "running" and _priority(other) < _priority(run)
        else:
            owns_before = other.status == "running" or _priority(other) < _priority(run)
        if owns_before:
            eligible.append(other)
    return min(eligible, key=lambda r: (r.status != "running", _priority(r))) if eligible else None
