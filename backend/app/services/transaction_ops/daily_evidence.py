"""Reuse scoped collected observations; saved evidence never authorizes a write."""

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.transaction_ops import TransactionRun

_REUSE_KEYS = ("reused_observation_run_ids", "reused_daily_run_ids")


def reuses_coverage(run):
    return any(key in (run.progress_json or {}) for key in _REUSE_KEYS)


def observation_basis(root):
    return root.params_json.get(
        "window_basis", "updated_at" if getattr(root, "origin", None) == "schedule" else "completed_at"
    )


def scheduled_observation_floor(root):
    """Original reads must be no earlier than this daily window's settle cutoff.

    The whole overlap is rechecked at that cutoff. A scan that started before
    it cannot replace this check merely because it finished afterwards.
    """
    end = datetime.fromisoformat(root.params_json["window_end"])
    value = root.config_snapshot.get("mapping_json", {}).get("reconciliation_policy")
    if value is None:
        return end
    from app.services.transaction_ops.periods import ReconciliationPolicy

    policy = ReconciliationPolicy.model_validate(value)
    settled = end.astimezone(ZoneInfo(policy.timezone_name)).replace(
        hour=policy.daily_check_hour, minute=0, second=0, microsecond=0
    )
    return max(end, settled.astimezone(timezone.utc))


def compatible_scope(root):
    r = TransactionRun
    snapshot = root.config_snapshot
    # Exact mapping identity is intentional: changing accounting policy cannot
    # certify a period using findings collected under the previous policy.
    predicates = [
        r.tenant_id == root.tenant_id,
        r.config_id == root.config_id,
        r.config_snapshot["mapping_json"] == snapshot.get("mapping_json", {}),
        func.coalesce(r.config_snapshot["evidence_contract_version"].astext, "1")
        == str(snapshot.get("evidence_contract_version", 1)),
        func.coalesce(r.params_json["window_basis"].astext, "updated_at") == observation_basis(root),
    ]
    for key in (
        "source_connection_id",
        "source_step_id",
        "netsuite_connection_id",
        "netsuite_account_id",
        "subsidiary_id",
        "record_type",
    ):
        predicates.append(r.config_snapshot[key].astext == snapshot.get(key))
    return predicates


def compatible_observation_runs(root, span):
    r = TransactionRun
    return [
        *compatible_scope(root),
        # Exact-order/recovery work is not complete period discovery.
        or_(
            (r.origin == "schedule") & r.params_json["review"].astext.is_(None),
            r.origin.in_(("manual", "chat")) & r.params_json["review"].astext.is_not(None),
        ),
        func.coalesce(r.params_json["order_references"], cast("[]", JSONB)) == [],
        cast(r.params_json["window_start"].astext, DateTime(timezone=True)) >= span.start,
        cast(r.params_json["window_end"].astext, DateTime(timezone=True)) <= span.end,
        cast(r.params_json["window_start"].astext, DateTime(timezone=True))
        < cast(r.params_json["window_end"].astext, DateTime(timezone=True)),
    ]


def compatible_daily_runs(root, span):
    r = TransactionRun
    return [
        *compatible_observation_runs(root, span),
        r.origin == "schedule",
        func.coalesce(r.config_snapshot["destination_discovery_version"].astext, "1")
        == str(root.config_snapshot.get("destination_discovery_version", 1)),
    ]


def scan_complete(run):
    progress = run.progress_json or {}
    return (
        run.status == "finished"
        and run.termination_reason == "done"
        and progress.get("scan_complete") is True
        and progress.get("refund_scan_complete") is True
        and (
            run.params_json.get("window_basis", "updated_at") != "updated_at"
            or (
                progress.get("destination_scan_complete") is True
                and (
                    run.config_snapshot.get("destination_discovery_version", 1) < 2
                    or (
                        progress.get("dependency_scan_complete") is True
                        and progress.get("dependency_index_seed", {}).get("complete") is True
                    )
                )
            )
        )
    )


async def completed_daily_windows(db, root, span):
    return await completed_observation_windows(db, root, span, daily_only=True)


async def completed_observation_windows(db, root, span, *, daily_only=False, source_ids=None, now=None):
    r = TransactionRun
    query = select(
        r.id,
        r.params_json["window_start"].astext.label("window_start"),
        r.params_json["window_end"].astext.label("window_end"),
    ).where(
        *(compatible_daily_runs(root, span) if daily_only else compatible_observation_runs(root, span)),
        r.status == "finished",
        r.termination_reason == "done",
        r.progress_json["scan_complete"].astext == "true",
        r.progress_json["refund_scan_complete"].astext == "true",
        or_(
            func.coalesce(r.config_snapshot["destination_discovery_version"].astext, "1") == "1",
            r.params_json["window_basis"].astext == "completed_at",
            and_(
                r.progress_json["dependency_scan_complete"].astext == "true",
                r.progress_json["dependency_index_seed"]["complete"].astext == "true",
            ),
        ),
        # A saved-report receipt is not a new observation. Always resolve
        # coverage from provider-backed scans, before applying the row limit.
        *(r.progress_json[key].astext.is_(None) for key in _REUSE_KEYS),
    )
    if observation_basis(root) == "updated_at":
        query = query.where(r.progress_json["destination_scan_complete"].astext == "true")
    if getattr(root, "origin", None) == "schedule":
        # Reusing historical coverage advances discovery, never the observation
        # clock. A daily job must meet its own exact discovery contract, and the
        # original scan must have finished after the closed window it covers.
        observed_since = func.coalesce(
            cast(r.progress_json["continuation_started_at"].astext, DateTime(timezone=True)), r.created_at
        )
        query = query.where(
            # Explicit retries and cross-cycle scheduled resumes reset their
            # scan clock while retaining old cursors. Without walking that
            # lineage, they cannot prove every original read met this floor.
            r.progress_json["review_attempt"].astext.is_(None),
            r.progress_json["evidence_root_id"].astext.is_(None),
            observed_since >= scheduled_observation_floor(root),
            r.finished_at >= observed_since,
            func.coalesce(r.config_snapshot["destination_discovery_version"].astext, "1")
            == str(root.config_snapshot.get("destination_discovery_version", 1)),
            r.finished_at >= cast(r.params_json["window_end"].astext, DateTime(timezone=True)),
            r.finished_at <= (now or datetime.now(timezone.utc)),
        )
    if source_ids is not None:
        query = query.where(r.id.in_([UUID(str(value)) for value in source_ids]))
    # V2 daily receipts require every dependency stream; historical report
    # reuse still preserves the old observation contract without claiming a new scan.
    if (daily_only or getattr(root, "origin", None) == "schedule") and root.config_snapshot.get(
        "destination_discovery_version", 1
    ) >= 2:
        query = query.where(
            r.progress_json["dependency_scan_complete"].astext == "true",
            r.progress_json["dependency_index_seed"]["complete"].astext == "true",
        )
    rows = (await db.execute(query.order_by(r.created_at.desc()).limit(512))).all()
    return [
        (
            datetime.fromisoformat(r.window_start),
            datetime.fromisoformat(r.window_end),
            str(r.id),
        )
        for r in rows
    ]


def covered_until(start, end, windows):
    cursor = start
    for lower, upper, *_ in sorted(windows):
        if lower > cursor:
            break
        if upper > cursor:
            cursor = min(upper, end)
        if cursor == end:
            break
    return cursor


def covered_days(start, end, windows, timezone_name="America/Los_Angeles"):
    zone = ZoneInfo(timezone_name)
    count, cursor = 0, start
    while cursor < end:
        following = min(end, (cursor.astimezone(zone) + timedelta(days=1)).astimezone(timezone.utc))
        count += covered_until(cursor, following, windows) == following
        cursor = following
    return count


async def coverage_receipt(db, run, span, *, whole_span=False, source_ids=None, now=None):
    """One coverage proof for manual reviews and daily catch-up; no provider I/O.

    A receipt refers directly to real completed observations, never other
    receipts. It cannot fill a gap, renew evidence, or supply write authority.
    """
    if run.params_json.get("order_references"):
        return None
    windows = await completed_observation_windows(db, run, span, source_ids=source_ids, now=now)
    start, end = (
        (span.start, span.end)
        if whole_span
        else (
            datetime.fromisoformat(run.params_json["window_start"]),
            datetime.fromisoformat(run.params_json["window_end"]),
        )
    )
    if covered_until(start, end, windows) != end:
        return None
    ids = [UUID(row[2]) for row in windows]
    sources = (
        await db.execute(
            select(
                TransactionRun.id,
                TransactionRun.origin,
                TransactionRun.finished_at,
                TransactionRun.config_snapshot["destination_discovery_version"].astext.label("discovery_version"),
            ).where(TransactionRun.tenant_id == run.tenant_id, TransactionRun.id.in_(ids))
        )
    ).all()
    receipt = {
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": True,
        "pending_refs": [],
        "reused_observation_run_ids": [str(row.id) for row in sources],
        "reused_daily_run_ids": [
            str(row.id)
            for row in sources
            if row.origin == "schedule"
            and int(row.discovery_version or 1) == run.config_snapshot.get("destination_discovery_version", 1)
        ],
        "reused_scan_completed_at": max((row.finished_at for row in sources if row.finished_at), default=None),
    }
    if receipt["reused_scan_completed_at"] is not None:
        receipt["reused_scan_completed_at"] = receipt["reused_scan_completed_at"].isoformat()
    if run.params_json.get("review"):
        receipt["review_coverage_complete"] = covered_until(span.start, span.end, windows) == span.end
    return receipt
