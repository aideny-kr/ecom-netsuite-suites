"""Bounded, tenant-scoped operational evidence. Never probes or dispatches work."""

from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionConfig, TransactionRun
from app.services.transaction_ops import daily_status, state_service
from app.services.transaction_ops.auth_recovery import auth_stop
from app.services.transaction_ops.continuation import (
    MAX_CYCLE_AGE,
    MAX_PARTS,
    READ_RETRY_DELAYS,
    SCHEDULE_MAX_PARTS,
    continuation_result,
    next_metadata,
    read_retry_due,
    scheduled_part_resume_candidate,
    scheduled_read_stop,
)
from app.services.transaction_ops.freshness import freshness
from app.services.transaction_ops.periods import ReconciliationPolicy

_ACTIVE_LIMIT = 5
_COUNTERS = ("processed", "scan_count", "refund_scan_count", "destination_scan_count", "dependency_step_count")
_CURSORS = ("page", "last_source_id", "refund_after_id", "destination_after_id")


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) and value.utcoffset() is not None else None


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _code(value):
    return value if isinstance(value, str) and len(value) <= 100 and value.replace("_", "").isalnum() else None


def _timestamp(value):
    try:
        return _iso(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _error_stopped_run(run, failure):
    if run.status != "finished" or run.termination_reason != "error" or failure.get("resolved") is not False:
        return False
    try:
        return (
            failure.get("run_id", str(run.id)) == str(run.id)
            and run.created_at <= datetime.fromisoformat(failure["observed_at"]) <= run.finished_at
        )
    except (KeyError, TypeError, ValueError):
        return False


def schedule(config, now):
    policy_value = (config.mapping_json or {}).get("reconciliation_policy")
    active = config.enabled and config.schedule_enabled
    if policy_value is not None:
        policy = ReconciliationPolicy.model_validate(policy_value)
        zone = ZoneInfo(policy.timezone_name)
        day = now.astimezone(zone).date()
        check = datetime.combine(day, time(policy.daily_check_hour), zone).astimezone(timezone.utc)
        if check <= now:
            check = datetime.combine(day + timedelta(days=1), time(policy.daily_check_hour), zone).astimezone(
                timezone.utc
            )
        return {
            "enabled": active,
            "kind": "daily",
            "timezone": policy.timezone_name,
            "next_check_at": _iso(check) if active else None,
        }
    seconds = config.interval_minutes * 60
    check = datetime.fromtimestamp((int(now.timestamp()) // seconds + 1) * seconds, timezone.utc)
    return {"enabled": active, "kind": "interval", "timezone": "UTC", "next_check_at": _iso(check) if active else None}


def continuation_status(run, now, *, blocked=None):
    """Reuse the finite continuation contract; eligibility is not a dispatch receipt."""
    eligibility = now
    state, reason = "eligible", "productive_checkpoint"
    try:
        next_metadata(run, now)
    except (ValueError, TypeError, KeyError) as exc:
        reason = str(exc)
        if reason != "read_retry_wait":
            return {"state": "blocked", "reason": _code(reason) or "invalid_checkpoint", "eligible_at": None}
        count = run.progress_json.get("continuation_read_retry_count", 0)
        eligibility = run.finished_at + READ_RETRY_DELAYS[count]
        # A backoff ending after the finite cycle expires is not an eligible retry.
        try:
            next_metadata(run, eligibility)
        except (ValueError, TypeError, KeyError) as later:
            return {"state": "blocked", "reason": _code(str(later)) or "invalid_checkpoint", "eligible_at": None}
        state = "waiting_for_retry"
    if blocked and not (
        blocked.get("reason") == "no_progress"
        and read_retry_due(run, eligibility)
        or blocked.get("reason") == "part_limit"
        and scheduled_part_resume_candidate(run, eligibility)
    ):
        return {"state": "blocked", "reason": _code(blocked.get("reason")), "eligible_at": None}
    if auth_stop(run):
        # Credential inspection belongs to the scheduler, never this read tool.
        return {"state": "connection_check_required", "reason": "authentication_rejected", "eligible_at": None}
    return {"state": state, "reason": reason, "eligible_at": _iso(eligibility)}


def run_snapshot(run, now):
    progress = run.progress_json or {}
    execution = run.status
    if run.status == "pending":
        deadline = state_service._first_claim_deadline(run, now)
        if deadline is None or deadline <= now:
            execution = "queue_expired"
    if run.status == "running":
        if run.deadline_at <= now:
            execution = "deadline_expired"
        elif progress.get("worker_yielded_at") and run.lease_token is None:
            execution = "queued"
        elif run.lease_until is None or run.lease_until <= now:
            execution = "lease_expired"
    failure = progress.get("last_read_failure")
    diagnostic = None
    if isinstance(failure, dict):
        diagnostic = {
            "code": _code(failure.get("code")),
            "stage": _code(failure.get("stage")),
            "observed_at": _timestamp(failure.get("observed_at")),
            "resolved": failure.get("resolved") if type(failure.get("resolved")) is bool else None,
            "blocking": bool(scheduled_read_stop(run) or _error_stopped_run(run, failure)),
        }
    return {
        "run_id": str(run.id),
        "origin": run.origin,
        "status": run.status,
        "execution_state": execution,
        "termination_reason": run.termination_reason,
        "phase": _code(progress.get("phase")),
        "window_start": (run.params_json or {}).get("window_start"),
        "window_end": (run.params_json or {}).get("window_end"),
        "run_state_updated_at": _iso(run.updated_at),
        "last_progress_at": None,
        "lease_until": _iso(run.lease_until),
        "deadline_at": _iso(run.deadline_at),
        "counters": {key: _number(progress.get(key)) for key in _COUNTERS},
        "cursor": {key: _number(progress.get(key)) for key in _CURSORS if key in progress},
        "financial_counts": {
            "scope": "run_checkpoint",
            **{key: _number(progress.get(key)) for key in ("matched", "needs_review", "not_verified")},
        },
        "budget": {
            "api_calls_used": run.api_calls_used,
            "api_calls_held": run.api_calls_held,
            "max_api_calls": run.max_api_calls,
            "orders_used": run.orders_used,
            "max_orders": run.max_orders,
        },
        "lineage": {
            "root_run_id": progress.get("continuation_root_id") or str(run.id),
            "part": _number(progress.get("continuation_part", 1)),
            "max_parts": SCHEDULE_MAX_PARTS if run.origin == "schedule" else MAX_PARTS,
            "max_cycle_seconds": int(MAX_CYCLE_AGE.total_seconds()),
            "read_retries_used": _number(progress.get("continuation_read_retry_count", 0)),
            "max_read_retries": len(READ_RETRY_DELAYS),
        },
        "last_read_failure": diagnostic,
        "collection_wait": None,
    }


def _action(kind, reason, eligible_at=None):
    return {"kind": kind, "reason": reason, "eligible_at": eligible_at, "dispatch_verified": False}


def _next_action(config, latest, active, coverage, planned, continuation, now):
    if not planned["enabled"]:
        return _action("paused", "schedule_disabled")
    scheduled = next((r for r in active if r["origin"] in {"schedule", "recovery"}), None)
    if scheduled:
        if scheduled["execution_state"] in {"lease_expired", "deadline_expired", "queue_expired"}:
            return _action("scheduler_recovery", scheduled["execution_state"], _iso(now))
        if scheduled["collection_wait"]:
            return _action("collection_recheck", "recorded_collection_wait", _iso(now))
        if scheduled["status"] == "pending":
            return _action("dispatch_pending", "queued_run", _iso(now))
        return _action("work_in_progress", "active_lease")
    if continuation:
        if continuation["state"] in {"eligible", "waiting_for_retry"}:
            return _action("continue_checkpoint", continuation["reason"], continuation["eligible_at"])
        if continuation["state"] == "connection_check_required":
            return _action("check_connection", continuation["reason"])
        if auth_stop(latest) or continuation["reason"] not in {
            "part_limit",
            "cycle_expired",
            "no_progress",
            "read_retry_limit",
            "paused",
            "feature_unavailable",
            "permission_denied",
            "continuation_unavailable",
        }:
            return _action("operator_review", continuation["reason"])
        # A finite continuation stop is not a permanent stop of the next daily cycle.
        from app.services.transaction_ops.scheduler import _cycle_key, _schedule_key

        if latest and _cycle_key(config, latest) < _schedule_key(config, now):
            return _action("new_schedule_cycle", continuation["reason"], _iso(now))
        return _action("scheduled_check", continuation["reason"], planned["next_check_at"])
    from app.services.transaction_ops.scheduler import _cycle_key, _schedule_key, _scope

    if (
        latest
        and _cycle_key(config, latest) >= _schedule_key(config, now)
        and not (planned["kind"] == "daily" and latest.termination_reason == "done")
    ):
        return _action(
            "scheduled_check", latest.termination_reason or "cycle_already_started", planned["next_check_at"]
        )

    _, _, reason = _scope(config, latest, now)
    if reason == "waiting_for_daily_cutoff":
        if coverage["status"] not in {"up_to_date", "paused"}:
            return _action("verify_coverage", "completed_window_has_no_verified_coverage")
        return _action("scheduled_check", reason, planned["next_check_at"])
    if reason:
        return _action("operator_review", reason)
    return _action("catch_up" if latest else "initial_scan", "scheduler_scope_due", _iso(now))


async def operational_status(db, tenant_id, *, config_id=None, limit=20, offset=0, daily_only=False, now=None):
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
        raise ValueError("invalid_status_scope")
    now = await state_service.run_clock(db, now)
    await set_tenant_context(db, str(tenant_id))
    c, r = TransactionConfig, TransactionRun
    query = select(c).where(c.tenant_id == tenant_id, state_service.current_config_clause())
    if daily_only:
        query = query.where(
            c.enabled.is_(True),
            c.schedule_enabled.is_(True),
            c.mapping_json["reconciliation_policy"].astext.is_not(None),
        )
    if config_id is not None:
        query = query.where(c.id == UUID(str(config_id)))
    configs = list(await db.scalars(query.order_by(c.created_at, c.id).offset(offset).limit(limit + 1)))
    truncated, configs = len(configs) > limit, configs[:limit]
    result = {
        "observed_at": _iso(now),
        "source": "stored_reconciliation_state",
        "entities": [],
        "truncated": truncated,
        "next_offset": offset + limit if truncated else None,
    }
    if not configs:
        return result
    ids = [c.id for c in configs]
    calendar = [c for c in configs if (c.mapping_json or {}).get("reconciliation_policy") is not None]
    coverage = {
        item["config_id"]: item for item in await daily_status.daily_status(db, tenant_id, now=now, configs=calendar)
    }
    latest = {
        row.config_id: row
        for row in await db.scalars(
            select(r)
            .where(r.tenant_id == tenant_id, r.config_id.in_(ids), r.origin == "schedule")
            .distinct(r.config_id)
            .order_by(r.config_id, r.created_at.desc(), r.params_json["evaluation_key"].astext.desc(), r.id.desc())
        )
    }
    ranked = (
        select(
            r.id,
            func.row_number()
            .over(
                partition_by=r.config_id,
                order_by=(r.origin.notin_(("schedule", "recovery")), r.status != "running", r.created_at, r.id),
            )
            .label("position"),
        )
        .where(r.tenant_id == tenant_id, r.config_id.in_(ids), r.status.in_(("pending", "running")))
        .subquery()
    )
    active = list(
        await db.scalars(
            select(r)
            .join(ranked, ranked.c.id == r.id)
            .where(r.tenant_id == tenant_id, ranked.c.position <= _ACTIVE_LIMIT + 1)
            .order_by(r.config_id, ranked.c.position)
        )
    )
    owner_ids = set()
    for row in active:
        try:
            owner_ids.add(UUID(str((row.progress_json or {}).get("collection_wait", {}).get("run_id"))))
        except (TypeError, ValueError, AttributeError):
            pass
    owners = (
        {
            (row.config_id, row.id): row
            for row in await db.scalars(
                select(r).where(r.tenant_id == tenant_id, r.config_id.in_(ids), r.id.in_(owner_ids))
            )
        }
        if owner_ids
        else {}
    )
    for config in configs:
        planned = schedule(config, now)
        cover = coverage.get(
            str(config.id),
            {
                "status": "not_applicable",
                "checked_through": None,
                "expected_until": None,
                "reason": "interval_schedule",
            },
        )
        expected = cover.get("expected_until")
        cover["expected_checked_through"] = (
            (datetime.fromisoformat(expected).astimezone(ZoneInfo(planned["timezone"])) - timedelta(microseconds=1))
            .date()
            .isoformat()
            if expected
            else None
        )
        running = [row for row in active if row.config_id == config.id]
        snapshots = []
        for row in running[:_ACTIVE_LIMIT]:
            snapshot = run_snapshot(row, now)
            wait = (row.progress_json or {}).get("collection_wait")
            if isinstance(wait, dict):
                try:
                    owner = owners.get((config.id, UUID(str(wait.get("run_id")))))
                except (TypeError, ValueError):
                    owner = None
                snapshot["collection_wait"] = {
                    "basis": "recorded_wait_requires_scheduler_recheck",
                    "owner": {
                        "run_id": str(owner.id),
                        "status": owner.status,
                        "termination_reason": owner.termination_reason,
                    }
                    if owner
                    else None,
                }
            snapshots.append(snapshot)
        last = latest.get(config.id)
        recovery = None
        if last and last.status == "finished" and (last.termination_reason == "budget" or auth_stop(last)):
            child, blocked = await continuation_result(db, tenant_id, last.id)
            recovery = (
                {"state": "continued", "reason": "child_exists", "eligible_at": None}
                if child
                else continuation_status(last, now, blocked=blocked)
            )
        entity = {
            "config_id": str(config.id),
            "name": config.name,
            "subsidiary_id": config.subsidiary_id,
            "coverage": cover,
            "schedule": planned,
            "latest_schedule": run_snapshot(last, now) if last else None,
            "active_runs": snapshots,
            "active_runs_truncated": len(running) > _ACTIVE_LIMIT,
            "continuation": recovery,
            "next_action": _next_action(config, last, snapshots, cover, planned, recovery, now),
        }
        policy = ReconciliationPolicy.model_validate((config.mapping_json or {}).get("reconciliation_policy") or {})
        entity["freshness"] = freshness(
            entity, daily_check_hour=policy.daily_check_hour, now=now, configured_at=config.created_at
        )
        result["entities"].append(entity)
    return result


def chat_table(result):
    """A compact decision table rendered by the existing chat table interceptor."""
    rows = []
    for entity in result["entities"]:
        coverage, action = entity["coverage"], entity["next_action"]
        active = entity["active_runs"]
        work = (
            ", ".join(f"{r['origin']}: {r['execution_state']} ({r['phase'] or 'phase unknown'})" for r in active)
            or "No active run"
        )
        if entity["active_runs_truncated"]:
            work += "; more active runs omitted"
        counts = (entity.get("latest_schedule") or {}).get("financial_counts") or {}
        financial = "; ".join(
            f"{counts.get(key) if counts.get(key) is not None else 'unknown'} {label}"
            for key, label in (("matched", "matched"), ("needs_review", "need review"), ("not_verified", "unverified"))
        )
        rows.append(
            [
                entity["name"],
                coverage["status"],
                coverage.get("checked_through"),
                coverage.get("expected_checked_through"),
                work,
                financial,
                action["kind"],
                action["reason"],
                action["eligible_at"],
            ]
        )
    return {
        "columns": [
            "Entity",
            "Scan coverage",
            "Latest verified day",
            "Expected day",
            "Current work",
            "Latest scheduled checkpoint",
            "Next action",
            "Reason",
            "Eligible at (UTC)",
        ],
        "rows": rows,
        "row_count": len(rows),
        "query": "",
        "suppress_llm_value": True,
        "source_kind": "transaction_ops",
    }
