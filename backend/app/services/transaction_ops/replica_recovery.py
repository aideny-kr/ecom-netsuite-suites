"""One verified Metabase read recovery per real schedule cycle, never writes.

A successful connection Test is a durable wake-up, not evidence that a scan
completed. Recovery starts a separately bounded cycle from the exact checkpoint;
the exhausted parent's budgets and diagnostics remain untouched.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.models.mcp_connector import McpConnector
from app.models.transaction_ops import TransactionRun
from app.services.transaction_ops.metabase_reader import TRANSIENT_REPLICA_READ_CODES, ReplicaBinding

VERIFICATION_KEY = "transaction_replica_verification"
RECOVERABLE_CODES = TRANSIENT_REPLICA_READ_CODES | {
    "replica_authentication_required",
    "replica_connection_unavailable",
    "replica_contract_changed",
    "replica_schema_verification_required",
}


def replica_stop(previous):
    if previous is None:
        return False
    progress = getattr(previous, "progress_json", None) or {}
    failure = progress.get("last_read_failure") or {}
    params = getattr(previous, "params_json", None) or {}
    if (
        getattr(previous, "origin", None) != "schedule"
        or getattr(previous, "status", None) != "finished"
        or getattr(previous, "termination_reason", None) not in {"budget", "error"}
        or not isinstance(failure, dict)
        or failure.get("code") not in RECOVERABLE_CODES
        or failure.get("resolved") is not False
        or progress.get("restart_scan")
        or params.get("review")
        or params.get("operation_id")
        or params.get("evidence_mode", "current") != "current"
    ):
        return False
    try:
        observed = datetime.fromisoformat(failure["observed_at"])
        return (
            observed.utcoffset() is not None
            and previous.created_at <= observed <= previous.finished_at
            and failure.get("run_id", str(previous.id)) == str(previous.id)
            and progress.get("read_stop_run_id", str(previous.id)) == str(previous.id)
        )
    except (KeyError, TypeError, ValueError):
        return False


def recovery_key(previous, verification):
    return f"replica-reconnect:{previous.id}:{verification['id']}"


async def recovery_ready(db, tenant_id, previous, config, now, *, lock=False):
    """Inspect stored, scope-bound proof only; never poll a provider on Beat ticks."""
    from app.services.metabase_oauth_service import is_metabase
    from app.services.transaction_ops.runner import enabled
    from app.services.transaction_ops.scheduler import _schedule_key

    if not replica_stop(previous) or getattr(previous, "tenant_id", None) != tenant_id or config.tenant_id != tenant_id:
        return None
    if not config.enabled or not config.schedule_enabled or previous.config_id != config.id:
        return None
    snapshot = previous.config_snapshot or {}
    if (
        any(
            str(snapshot.get(field)) != str(getattr(config, field))
            for field in (
                "netsuite_connection_id",
                "netsuite_account_id",
                "subsidiary_id",
                "source_connection_id",
                "source_step_id",
            )
        )
        or snapshot.get("mapping_json") != config.mapping_json
    ):
        return None
    if (config.mapping_json or {}).get("action_mode", "detect_only") not in {"detect_only", "propose_actions"}:
        return None
    cycle = _schedule_key(config, now)
    prior = (previous.progress_json or {}).get("replica_recovery") or {}
    if not isinstance(prior, dict) or prior.get("cycle_key") == cycle:
        return None
    if not await enabled(db, tenant_id):
        return None
    try:
        binding = ReplicaBinding.model_validate((config.mapping_json or {})["metabase_replica"])
    except (KeyError, TypeError, ValueError):
        return None
    query = (
        select(McpConnector)
        .where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.id == binding.connector_id,
            McpConnector.status == "active",
            McpConnector.is_enabled.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        # Explicit Test/refresh can hold this row during provider I/O. Never
        # let one connector consume the entire multi-tenant collector tick.
        query = query.with_for_update(skip_locked=True)
    connector = await db.scalar(query)
    if (
        connector is None
        or not is_metabase(connector)
        or connector.server_url != binding.server_url
        or not connector.encrypted_credentials
    ):
        return None
    proof = (connector.metadata_json or {}).get(VERIFICATION_KEY)
    if (
        not isinstance(proof, dict)
        or proof.get("schema_version") != 1
        or proof.get("binding") != binding.model_dump(mode="json")
    ):
        return None
    try:
        identifier = str(UUID(proof["id"]))
        verified = datetime.fromisoformat(proof["verified_at"])
        observed = datetime.fromisoformat(previous.progress_json["last_read_failure"]["observed_at"])
        if verified.utcoffset() is None or not observed < verified <= now or now - verified > timedelta(days=1):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {"id": identifier, "verified_at": verified.isoformat(), "cycle_key": cycle}


async def assert_latest_idle(db, tenant_id, previous):
    """Called under the config lock shared with all other create paths."""
    latest = await db.scalar(
        select(TransactionRun.id)
        .where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.config_id == previous.config_id,
            TransactionRun.origin == "schedule",
        )
        .order_by(
            TransactionRun.created_at.desc(),
            TransactionRun.params_json["evaluation_key"].astext.desc(),
            TransactionRun.id.desc(),
        )
        .limit(1)
    )
    active = await db.scalar(
        select(TransactionRun.id)
        .where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.config_id == previous.config_id,
            TransactionRun.origin.in_(("schedule", "recovery")),
            TransactionRun.status.in_(("pending", "running")),
        )
        .limit(1)
    )
    return latest == previous.id and active is None
