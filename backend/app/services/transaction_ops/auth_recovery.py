"""Bounded recovery of native read authentication, never financial writes."""

import hashlib
import math
import re
from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import select

from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection

rejected_read_scope: ContextVar[tuple | None] = ContextVar("rejected_native_read_scope", default=None)

# Include failures before a provider request: an unhealthy connection or a
# failed token refresh can stop the reader without ever receiving HTTP 401.
# These remain non-transient; only a newer usable credential can earn a resume.
AUTH_STOP_CODES = (
    "netsuite_upstream_http_401",
    "netsuite_invalid_connection",
    "netsuite_authentication_failed",
)


def auth_stop(previous):
    if previous is None:
        return False
    progress = getattr(previous, "progress_json", None) or {}
    failure = progress.get("last_read_failure") or {}
    if (
        getattr(previous, "origin", None) != "schedule"
        or getattr(previous, "status", None) != "finished"
        or getattr(previous, "termination_reason", None) != "error"
        or not isinstance(failure, dict)
        or failure.get("code") not in AUTH_STOP_CODES
        or failure.get("resolved") is not False
        or progress.get("restart_scan")
        or (getattr(previous, "params_json", None) or {}).get("review")
        or (getattr(previous, "params_json", None) or {}).get("operation_id")
    ):
        return False
    try:
        observed = datetime.fromisoformat(failure["observed_at"])
        return previous.created_at <= observed <= previous.finished_at and failure.get(
            "run_id", str(previous.id)
        ) == str(previous.id)
    except (KeyError, TypeError, ValueError):
        return False


def auth_resume_candidate(previous):
    count = (getattr(previous, "progress_json", None) or {}).get("auth_resume_count", 0)
    return auth_stop(previous) and type(count) is int and count == 0


async def auth_resume_ready(db, tenant_id, previous, config, now):
    """Require a newer usable credential before spending the one lineage resume.

    This is not a declaration that the failed read succeeded: the child performs
    that read again under normal authorization/budgets. A second rejection stops.
    """
    if not auth_resume_candidate(previous):
        return False
    snapshot = previous.config_snapshot or {}
    if any(
        str(snapshot.get(field)) != str(getattr(config, field))
        for field in ("netsuite_connection_id", "netsuite_account_id", "subsidiary_id")
    ):
        return False
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.id == config.netsuite_connection_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .execution_options(populate_existing=True)
    )
    if connection is None:
        return False
    try:
        from app.services.transaction_ops.netsuite_reader import _account

        credentials = decrypt_credentials(connection.encrypted_credentials)
        if _account(credentials.get("account_id")) != _account(config.netsuite_account_id):
            return False
        token = credentials.get("access_token")
        expires = credentials.get("expires_at")
        if not isinstance(token, str) or not token or type(expires) not in (int, float) or not math.isfinite(expires):
            return False
        if expires <= now.timestamp() + 300:
            return False
        failure = previous.progress_json["last_read_failure"]
        rejected = failure.get("auth_token_sha256")
        if rejected:
            return (
                isinstance(rejected, str)
                and re.fullmatch(r"[0-9a-f]{64}", rejected) is not None
                and failure.get("auth_connection_id") == str(connection.id)
                and failure.get("auth_account_id") == _account(config.netsuite_account_id)
                and hashlib.sha256(token.encode()).hexdigest() != rejected
            )
        # Legacy REST credentials used Oracle's fixed 3600s lifetime. Never use
        # updated_at: health checks touch it without rotating any credential.
        issued = credentials.get("issued_at", expires - 3600)
        return (
            type(issued) in (int, float)
            and math.isfinite(issued)
            and datetime.fromisoformat(failure["observed_at"]).timestamp() < issued <= now.timestamp()
        )
    except (KeyError, TypeError, ValueError):
        return False
