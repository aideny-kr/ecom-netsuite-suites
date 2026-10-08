"""Safe terminal diagnostics and finite recovery of scheduled collection only."""

from datetime import datetime
from pathlib import PurePath
from uuid import UUID

from sqlalchemy.exc import DBAPIError

from app.services.transaction_ops.read_recovery import TRANSIENT_READ_CODES, safe_read_code, transient_read_code

_CATEGORIES = (TimeoutError, ConnectionError, ValueError, TypeError, KeyError, RuntimeError, DBAPIError)
_CODES = TRANSIENT_READ_CODES | {"collection_database_unavailable", "collection_unexpected", "collection_permanent"}
_STAGES = {
    "collection",
    "enablement",
    "source_mirror",
    "source_snapshot",
    "get_config",
    "reserve_budget",
    "settle_budget",
    "update_progress",
    "record_finding",
    "record_finding_batch",
    "propose",
    "source_page",
    "source_order",
    "source_prepare_batch",
    "source_snapshot_batch",
    "source_refunds",
    "netsuite_order",
    "netsuite_refunds",
    "netsuite_orders_batch",
    "netsuite_refunds_batch",
    "commercial_credit",
    "create_preview",
    "guard_snapshot",
    "celigo_error",
    "dependency_page",
    "dependency_owners",
    "refund_page",
    "replica_page",
}


def collection_only(run):
    params = getattr(run, "params_json", None) or {}
    mapping = (getattr(run, "config_snapshot", None) or {}).get("mapping_json") or {}
    return (
        getattr(run, "origin", None) == "schedule"
        and mapping.get("action_mode", "detect_only") in {"detect_only", "propose_actions"}
        and not any(params.get(key) for key in ("operation_id", "verification_scope", "approval_message_id", "review"))
    )


def failure_diagnostic(exc, *, run_id, now, stage):
    """Never inspect exception text, args, SQL, HTTP bodies or provider headers."""
    code = transient_read_code(exc)
    if not code and isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        code = (
            "collection_database_unavailable"
            if exc.connection_invalidated
            or sqlstate in {"40001", "40P01", "53300", "57P01", "57P02", "57P03"}
            or isinstance(sqlstate, str)
            and sqlstate.startswith("08")
            else "collection_permanent"
        )
    if not code:
        code = "collection_unexpected" if safe_read_code(exc) == "unclassified_read_failure" else "collection_permanent"
    result = {
        "code": code,
        "category": next((cls.__name__ for cls in _CATEGORIES if isinstance(exc, cls)), "UnexpectedError"),
        "stage": stage if stage in _STAGES else "collection",
        "run_id": str(run_id),
        "observed_at": now.isoformat(),
    }
    # Only a repository-relative application filename and line, never traceback
    # locals, exception chains or an absolute path containing operator identity.
    trace = exc.__traceback__
    while trace:
        path = PurePath(trace.tb_frame.f_code.co_filename)
        if "transaction_ops" in path.parts and path.suffix == ".py" and path.stem.replace("_", "").isalnum():
            result["location"] = {"module": path.stem, "line": trace.tb_lineno}
        trace = trace.tb_next
    return result


def validated_failure(failure, run):
    """Validate terminal metadata before it reaches immutable evidence or audit."""
    if not isinstance(failure, dict) or failure.get("run_id") != str(run.id) or failure.get("code") not in _CODES:
        raise ValueError("invalid_collection_failure")
    observed = datetime.fromisoformat(failure["observed_at"])
    if observed.utcoffset() is None or failure.get("stage") not in _STAGES:
        raise ValueError("invalid_collection_failure")
    if failure.get("category") not in {cls.__name__ for cls in _CATEGORIES} | {"UnexpectedError"}:
        raise ValueError("invalid_collection_failure")
    result = {key: failure[key] for key in ("code", "category", "stage", "run_id", "observed_at")}
    location = failure.get("location") or {}
    if isinstance(location, dict) and isinstance(location.get("module"), str):
        module, line = location["module"], location.get("line")
        if len(module) <= 80 and module.replace("_", "").isalnum() and type(line) is int and 0 < line < 100000:
            result["location"] = {"module": module, "line": line}
    result["cursor"] = {
        key: value
        for key, value in (run.progress_json or {}).items()
        if key in {"page", "last_source_id", "refund_after_id", "destination_after_id", "processed"}
        and type(value) is int
        and value >= 0
    }
    return result


def collection_stop(run, *, operator_retry=False):
    if (
        not collection_only(run)
        or getattr(run, "status", None) != "finished"
        or getattr(run, "termination_reason", None) != "error"
    ):
        return False
    from app.services.transaction_ops.auth_recovery import auth_stop

    if auth_stop(run):
        return False  # Native authentication retains credential-gated recovery.
    failure = (getattr(run, "progress_json", None) or {}).get("last_collection_failure")
    if operator_retry and not failure:
        return run.termination_reason == "error"
    if not isinstance(failure, dict) or failure.get("run_id") != str(run.id):
        return False
    try:
        UUID(failure["run_id"])
        observed = datetime.fromisoformat(failure["observed_at"])
        return run.created_at <= observed <= run.finished_at and failure.get("code") in _CODES
    except (KeyError, TypeError, ValueError):
        return False
