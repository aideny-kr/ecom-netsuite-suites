"""Read-only connection diagnostics. A local read is not a remote health check."""

import math
import time

from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector


def _text(value):
    return str(value)[:500] if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def connection_snapshot(row):
    metadata = row.metadata_json or {}
    credentials = {}
    failed = False
    expired = False
    if row.encrypted_credentials:
        try:
            credentials = decrypt_credentials(row.encrypted_credentials)
            expiry = credentials.get("expires_at")
            if expiry is not None:
                expiry = float(expiry)
                if not math.isfinite(expiry):
                    raise ValueError("Invalid expiry")
                expired = time.time() > expiry
        except Exception:
            failed = True
            credentials = {}
    reported = row.status
    error = row.error_reason
    if failed:
        reported, error = "error", "Saved credentials could not be read. Reconnect this access method."
    elif expired and reported == "active":
        reported = "refresh_required" if credentials.get("refresh_token") else "needs_reauth"
    elif row.auth_type == "oauth2" and not row.encrypted_credentials:
        reported = "needs_reauth"
    if isinstance(row, McpConnector) and row.is_enabled is False:
        reported = "disabled"
    verification = metadata.get("verification_status")
    if verification not in {"ok", "partial", "error", "unsupported"}:
        verification = None
    return {
        "id": str(row.id),
        "label": row.label or row.provider,
        "provider": row.provider,
        "status": reported,
        "auth_type": row.auth_type,
        "token_expired": expired,
        "last_health_check": row.last_health_check_at.isoformat() if row.last_health_check_at else None,
        "verification_status": verification,
        "verification_at": _text(metadata.get("verification_at")),
        "account_identity": _text(
            metadata.get("account_id") or credentials.get("account_id") or metadata.get("project_id")
        ),
        "access_scope": _text(credentials.get("scope") or metadata.get("scope")),
        "role": _text(credentials.get("role_id") or metadata.get("role_id")),
        "error_reason": error,
        "client_id": _text(metadata.get("client_id") or credentials.get("client_id")),
        "restlet_url": _text(metadata.get("restlet_url")),
        "tool_count": len(row.discovered_tools or []) if isinstance(row, McpConnector) else None,
    }
