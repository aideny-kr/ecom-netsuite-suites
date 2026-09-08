"""Bounded Beat collection for opted-in investigations; publishes read jobs only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, String, and_, cast, exists, extract, func, literal, or_, select
from sqlalchemy.orm import aliased

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.services import feature_flag_service
from app.services.ingestion.solidus_dispatch import refresh_due_sources as _refresh_sources
from app.workers.celery_app import celery_app

_SCAN_LIMIT = 200
_DISPATCH_TIMEOUT = 5
_BROKER_IO_TIMEOUT = 1
_TICK_TIMEOUT = 40
_MAX_WINDOW = timedelta(days=31)


def _dependencies():
    from app.models.transaction_ops import TransactionConfig, TransactionRun
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service

    return state_service, RunCreate, TransactionConfig, TransactionRun


def _bucket(now, interval_minutes):
    seconds = interval_minutes * 60
    start = datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, timezone.utc)
    return "schedule:" + start.isoformat()


async def _recovery_ids(db, tenant_id, now):
    _, _, config, run = _dependencies()
    await set_tenant_context(db, str(tenant_id))
    child = aliased(run)
    has_child = exists(
        select(child.id).where(
            child.tenant_id == tenant_id, child.progress_json["continuation_of"].astext == cast(run.id, String)
        )
    )
    blocked = exists(
        select(AuditEvent.id).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == "transaction_ops.run.continuation_blocked",
            AuditEvent.resource_type == run.__tablename__,
            AuditEvent.resource_id == cast(run.id, String),
        )
    )
    has_review_child = exists(
        select(child.id).where(
            child.tenant_id == tenant_id,
            child.config_id == run.config_id,
            child.params_json["review"] == run.params_json["review"],
            cast(child.params_json["window_start"].astext, DateTime(timezone=True))
            == cast(run.params_json["window_end"].astext, DateTime(timezone=True)),
        )
    )
    review_enabled = exists(
        select(config.id).where(config.tenant_id == tenant_id, config.id == run.config_id, config.enabled.is_(True))
    )
    query = (
        select(run.id)
        .where(
            run.tenant_id == tenant_id,
            or_(
                and_(
                    run.status.in_(("pending", "running")),
                    or_(
                        run.status == "pending",
                        run.lease_until.is_(None),
                        run.lease_until <= now,
                        run.deadline_at <= now,
                    ),
                ),
                and_(
                    run.status == "finished",
                    run.termination_reason == "done",
                    run.params_json["review"].astext.is_not(None),
                    run.progress_json["scan_complete"].astext == "true",
                    run.progress_json["refund_scan_complete"].astext == "true",
                    cast(run.params_json["window_end"].astext, DateTime(timezone=True))
                    < cast(run.params_json["review"]["end"].astext, DateTime(timezone=True)),
                    ~has_review_child,
                    ~blocked,
                    review_enabled,
                ),
                and_(
                    run.status == "finished",
                    run.termination_reason == "budget",
                    run.origin != "recovery",
                    run.finished_at > now - timedelta(days=1),
                    or_(
                        run.progress_json["processed"].astext.notin_(("0", "")),
                        run.progress_json["scan_count"].astext.notin_(("0", "")),
                    ),
                    ~has_child,
                    ~blocked,
                ),
            ),
        )
        .order_by(run.created_at, run.id)
        .limit(_SCAN_LIMIT + 1)
    )
    # Expired deadlines deliberately remain eligible: claim_run persists their
    # budget termination, after which they disappear from this scan.
    return list((await db.execute(query)).scalars())


async def _candidate_ids(db, tenant_id, now):
    _, _, config, run = _dependencies()
    await set_tenant_context(db, str(tenant_id))
    latest = (
        select(run.config_id, func.max(run.created_at).label("latest_at"))
        .where(run.tenant_id == tenant_id, run.origin == "schedule")
        .group_by(run.config_id)
        .subquery()
    )
    active = exists(
        select(run.id).where(
            run.tenant_id == tenant_id, run.config_id == config.id, run.status.in_(("pending", "running"))
        )
    )
    interval_seconds = config.interval_minutes * 60
    bucket_epoch = func.floor(literal(int(now.timestamp())) / interval_seconds) * interval_seconds
    query = (
        select(config.id)
        .outerjoin(latest, latest.c.config_id == config.id)
        .where(
            config.tenant_id == tenant_id,
            config.enabled.is_(True),
            config.schedule_enabled.is_(True),
            ~active,
            or_(
                latest.c.latest_at.is_(None),
                extract("epoch", latest.c.latest_at) < bucket_epoch,
                config.mapping_json["reconciliation_policy"].astext.is_not(None),
            ),
        )
        .order_by(latest.c.latest_at.asc().nullsfirst(), config.created_at, config.id)
        .limit(_SCAN_LIMIT + 1)
    )
    # Filtering due intervals before the limit prevents frequently scanned but
    # not-yet-due scopes from starving others. Served scopes move to the end.
    return list((await db.execute(query)).scalars())


async def _schedule_history(db, tenant_id, config_id):
    _, _, _, run = _dependencies()
    await set_tenant_context(db, str(tenant_id))
    active = (
        await db.execute(
            select(run.id)
            .where(run.tenant_id == tenant_id, run.config_id == config_id, run.status.in_(("pending", "running")))
            .limit(1)
        )
    ).scalar_one_or_none()
    latest = await db.execute(
        select(run)
        .where(run.tenant_id == tenant_id, run.config_id == config_id, run.origin == "schedule")
        .order_by(run.created_at.desc(), run.params_json["evaluation_key"].astext.desc(), run.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return active is not None, latest.scalar_one_or_none()


def _scope(config, latest, now):
    """Return exact next scope, continuation id and a safe stall reason."""
    if latest and latest.termination_reason in {"budget", "stall", "error"}:
        params = latest.params_json
        scope = {key: params.get(key) for key in ("window_start", "window_end")}
        scope["order_references"] = params.get("order_references", [])
        return scope, latest.id if latest.termination_reason in {"budget", "stall"} else None, None
    policy_value = (getattr(config, "mapping_json", None) or {}).get("reconciliation_policy")
    if policy_value:
        from app.services.transaction_ops.periods import ReconciliationPolicy, scheduled_window

        try:
            last_end = datetime.fromisoformat(latest.params_json["window_end"]) if latest else None
            window = scheduled_window(ReconciliationPolicy.model_validate(policy_value), now, last_end)
        except (ValueError, TypeError, KeyError):
            return None, None, "invalid_reconciliation_policy"
        if window is None:
            return None, None, "waiting_for_daily_cutoff"
        return {"window_start": window[0], "window_end": window[1]}, None, None
    start = now - timedelta(minutes=config.interval_minutes)
    if latest:
        end = latest.params_json.get("window_end")
        if not end:
            return None, None, "scheduled_window_missing"
        start = datetime.fromisoformat(end)
        if start.utcoffset() is None:
            return None, None, "scheduled_window_invalid"
    if now - start > _MAX_WINDOW:
        return None, None, "window_gap_exceeds_limit"
    if start >= now:
        return None, None, "scheduled_window_not_ordered"
    return {"window_start": start, "window_end": now}, None, None


def publish_investigation(tenant_id, run_id, *, app=celery_app):
    # wait_for cannot cancel a blocking thread, and asyncio.run waits for its
    # executor during shutdown. Bound the real Redis sockets and connection
    # attempts as well; use a private connection so these options cannot leak
    # into the app's shared producer pool. Durable state supplies the result.
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
            "tasks.transaction_ops_run",
            kwargs={"tenant_id": str(tenant_id), "run_id": str(run_id)},
            queue="recon",
            retry=False,
            retry_policy={"max_retries": 0},
            connection=connection,
            ignore_result=True,
        )


async def _dispatch(tenant_id, run_id, stats):
    try:
        await asyncio.wait_for(
            asyncio.to_thread(publish_investigation, tenant_id, run_id),
            timeout=_DISPATCH_TIMEOUT,
        )
        stats["dispatched"] += 1
    except Exception:
        # A timeout may still have published. The durable run lease makes a
        # repeated publication safe; never remove a pending run here.
        stats["dispatch_failed"] += 1


async def collect_due_runs(db, now: datetime) -> dict:
    if now.utcoffset() is None:
        raise ValueError("An aware clock is required")
    now = now.astimezone(timezone.utc)
    state, request_type, _, _ = _dependencies()
    stats = {
        "source_refreshes": 0,
        "source_refresh_failed": 0,
        "tenants": 0,
        "created": 0,
        "recovered": 0,
        "dispatched": 0,
        "dispatch_failed": 0,
        "config_failed": 0,
        "tenant_failed": 0,
        "skipped": 0,
        "stalled": [],
        "truncated": False,
        "run_scan_limit": _SCAN_LIMIT,
        "config_scan_limit": _SCAN_LIMIT,
        "scan_limit_scope": "per_tenant",
    }
    try:
        async with asyncio.timeout(_TICK_TIMEOUT):
            tenants = await feature_flag_service.list_tenants_with_flags(db, ("celigo", "reconciliation"))
            if tenants:
                # A slow tenant must not consume the global time budget before
                # the same later tenants on every tick. No mutable cursor is
                # required; concurrent Beat processes choose the same order.
                offset = int(now.timestamp()) // 60 % len(tenants)
                tenants = tenants[offset:] + tenants[:offset]
            for tenant_id in tenants:
                stats["tenants"] += 1
                try:
                    stats["source_refreshes"] += await _refresh_sources(db, tenant_id, now)
                except Exception:
                    await db.rollback()
                    stats["source_refresh_failed"] += 1
                try:
                    recover = await _recovery_ids(db, tenant_id, now)
                    stats["truncated"] |= len(recover) > _SCAN_LIMIT
                    # Release a read transaction before waiting on a broker.
                    await db.commit()
                    for run_id in recover[:_SCAN_LIMIT]:
                        stats["recovered"] += 1
                        await _dispatch(tenant_id, run_id, stats)
                    candidates = await _candidate_ids(db, tenant_id, now)
                    stats["truncated"] |= len(candidates) > _SCAN_LIMIT
                    await db.commit()
                except Exception:
                    await db.rollback()
                    stats["tenant_failed"] += 1
                    continue
                for config_id in candidates[:_SCAN_LIMIT]:
                    try:
                        # Manual/chat creates take this same lock in state.
                        config = await state.get_config(db, tenant_id, config_id, lock=True)
                        active, latest = await _schedule_history(db, tenant_id, config_id)
                        key = _bucket(now, config.interval_minutes)
                        already_due = latest is not None and (
                            latest.params_json.get("evaluation_key") == key
                            or _bucket(latest.created_at, config.interval_minutes) >= key
                        )
                        policy_catchup = (
                            (getattr(config, "mapping_json", None) or {}).get("reconciliation_policy")
                            and latest is not None
                            and latest.termination_reason == "done"
                        )
                        if (
                            not config.enabled
                            or not config.schedule_enabled
                            or active
                            or (already_due and not policy_catchup)
                        ):
                            stats["skipped"] += 1
                            await db.commit()
                            continue
                        scope, resume_id, reason = _scope(config, latest, now)
                        if reason == "waiting_for_daily_cutoff":
                            stats["skipped"] += 1
                            await db.commit()
                            continue
                        if reason:
                            stats["stalled"].append(
                                {"tenant_id": str(tenant_id), "config_id": str(config_id), "reason": reason}
                            )
                            await db.commit()
                            continue
                        request = request_type(origin="schedule", evaluation_key=key, **scope)
                        run = await state.create_run(
                            db, tenant_id, config_id, request, actor=None, now=now, resume_from_run_id=resume_id
                        )
                        # create_run commits before returning. Only scalar IDs
                        # survive into the broker call or the next transaction.
                        run_id, status = run.id, run.status
                        stats["created"] += 1
                        if status == "pending":
                            await _dispatch(tenant_id, run_id, stats)
                    except Exception:
                        await db.rollback()
                        stats["config_failed"] += 1
    except TimeoutError:
        await db.rollback()
        stats["truncated"] = True
    return stats
