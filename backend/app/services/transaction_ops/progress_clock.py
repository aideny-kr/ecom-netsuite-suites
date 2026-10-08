"""An owned clock for committed evidence, independent of worker heartbeats."""

from datetime import datetime, timedelta

from sqlalchemy import Float, case, cast, func, literal, or_
from sqlalchemy.dialects.postgresql import JSONB

from app.models.transaction_ops import TransactionRun

COUNTERS = (
    "processed",
    "scan_count",
    "refund_scan_count",
    "outside_scope",
    "destination_scan_count",
    "dependency_step_count",
)
PHASES = {"orders": 1, "refunds": 2, "destination": 3, "done": 4}
OWNED = {"last_progress_at", "execution_started_at"}
STAGNATION = timedelta(minutes=10)
QUEUE_DELAY = timedelta(minutes=15)


def _number(value):
    return value if type(value) in (int, float) and value >= 0 else 0


def committed_progress(previous, incoming, now):
    previous = previous or {}
    result = {k: v for k, v in incoming.items() if k not in OWNED}
    result.update({k: previous[k] for k in OWNED if k in previous})
    if any(_number(incoming.get(k)) > _number(previous.get(k)) for k in COUNTERS) or (
        PHASES.get(incoming.get("phase"), 0) > PHASES.get(previous.get("phase"), 0)
    ):
        result["last_progress_at"] = now.isoformat()
    return result


def progress_sql(incoming, now):
    """Keep the checkpoint UPDATE fenced and atomic, without another DB read."""
    old = TransactionRun.progress_json
    advanced = []
    for key in COUNTERS:
        prior = case((func.jsonb_typeof(old[key]) == "number", cast(old[key].astext, Float)), else_=0)
        advanced.append(literal(_number(incoming.get(key))) > prior)
    phase = case(*[(old["phase"].astext == name, rank) for name, rank in PHASES.items()], else_=0)
    advanced.append(literal(PHASES.get(incoming.get("phase"), 0)) > phase)
    owned = func.jsonb_strip_nulls(
        func.jsonb_build_object(
            "execution_started_at",
            old["execution_started_at"],
            "last_progress_at",
            case((or_(*advanced), literal(now.isoformat())), else_=old["last_progress_at"].astext),
        )
    )
    stamped = literal({k: v for k, v in incoming.items() if k not in OWNED}, type_=JSONB).op("||")(owned)
    return case((TransactionRun.origin == "schedule", stamped), else_=literal(incoming, type_=JSONB))


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def stalled_snapshot(run, now):
    """No automatic takeover: a live lease is still authoritative."""
    if run["origin"] != "schedule" or run.get("status") not in {"pending", "running"}:
        return None
    if run.get("collection_wait"):
        return None
    if run["status"] == "pending":
        started = timestamp(run.get("created_at"))
        return "queue_delayed" if started and now - started >= QUEUE_DELAY else None
    committed = timestamp(run.get("last_progress_at")) or timestamp(run.get("execution_started_at"))
    return "progress_stalled" if committed and now - committed >= STAGNATION else None
