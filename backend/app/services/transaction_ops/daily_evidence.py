"""Reuse scoped collected observations; saved evidence never authorizes a write."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, cast, func, or_, select

from app.models.transaction_ops import TransactionRun


def compatible_observation_runs(root, span):
    r = TransactionRun
    snapshot = root.config_snapshot
    # Exact mapping identity is intentional: changing accounting policy cannot
    # certify a period using findings collected under the previous policy.
    predicates = [
        r.tenant_id == root.tenant_id,
        r.config_id == root.config_id,
        # Only normal scoped scans contribute a historical cohort. Exact-order
        # investigations and recovery/write-verification runs are not discovery.
        or_(
            (r.origin == "schedule") & r.params_json["review"].astext.is_(None),
            r.origin.in_(("manual", "chat")) & r.params_json["review"].astext.is_not(None),
        ),
        r.config_snapshot["mapping_json"] == snapshot.get("mapping_json", {}),
        func.coalesce(r.config_snapshot["evidence_contract_version"].astext, "1")
        == str(snapshot.get("evidence_contract_version", 1)),
        func.coalesce(r.params_json["window_basis"].astext, "updated_at")
        == root.params_json.get("window_basis", "completed_at"),
        cast(r.params_json["window_start"].astext, DateTime(timezone=True)) >= span.start,
        cast(r.params_json["window_end"].astext, DateTime(timezone=True)) <= span.end,
        cast(r.params_json["window_start"].astext, DateTime(timezone=True))
        < cast(r.params_json["window_end"].astext, DateTime(timezone=True)),
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


def compatible_daily_runs(root, span):
    r = TransactionRun
    return [*compatible_observation_runs(root, span), r.origin == "schedule"]


def scan_complete(run):
    progress = run.progress_json or {}
    return (
        run.status == "finished"
        and run.termination_reason == "done"
        and progress.get("scan_complete") is True
        and progress.get("refund_scan_complete") is True
        and (
            run.params_json.get("window_basis", "updated_at") != "updated_at"
            or progress.get("destination_scan_complete") is True
        )
    )


async def completed_daily_windows(db, root, span):
    return await completed_observation_windows(db, root, span, daily_only=True)


async def completed_observation_windows(db, root, span, *, daily_only=False):
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
    )
    if root.params_json.get("window_basis", "completed_at") == "updated_at":
        query = query.where(r.progress_json["destination_scan_complete"].astext == "true")
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
