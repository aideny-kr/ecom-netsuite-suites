"""Tenant-bound HTTP GET tools backed by saved encrypted connections.

Arbitrary API paths require exact-call approval: GET alone cannot establish
that an unfamiliar server is side-effect free. No headers, credentials, host,
HTTP method, or request body can be supplied by the model.
"""

import asyncio
import json
import re
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services import http_connector_service as http

_NAME = re.compile(r"^http__([0-9a-f]{32})__get$")
_SECRET_KEYS = frozenset(
    {"password", "token", "access_token", "refresh_token", "api_key", "authorization", "cookie", "set-cookie", "secret"}
)
_MAX_RESULT_CHARS = 20000


def parse_name(name):
    match = _NAME.fullmatch(name)
    return UUID(hex=match[1]) if match else None


def _query(tenant_id):
    return select(Connection).where(
        Connection.tenant_id == tenant_id,
        Connection.provider.in_(http.HTTP_PROVIDERS),
        Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
    )


async def build_definitions(db, tenant_id):
    rows = (await db.execute(_query(tenant_id).order_by(Connection.id))).scalars().all()
    definitions = []
    for row in rows:
        metadata = row.metadata_json or {}
        example = metadata.get("test_path") or (
            "sync/orders?per_page=1&page=1"
            if metadata.get("api_profile") == "framework_sync"
            else "orders?per_page=1&page=1"
        )
        definitions.append(
            {
                "name": f"http__{row.id.hex}__get",
                "description": (
                    f"[{row.provider}: {row.label}] GET JSON from the connected API at {metadata.get('base_url', '')}. "
                    f"Known read endpoint: {example}. Use documented relative endpoints; do not invent API schemas. "
                    "Requires human approval of the exact request. Credentials are supplied server-side. "
                    "Use pagination or filters for large results. GET only; no POST, PATCH, or DELETE."
                ),
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {
                            "type": "string",
                            "maxLength": 2048,
                            "description": "Relative endpoint with optional query parameters. No leading slash or URL.",
                        }
                    },
                    "required": ["path"],
                },
            }
        )
    return definitions


async def describe_target(name, db, tenant_id):
    row = (await db.execute(_query(tenant_id).where(Connection.id == parse_name(name)))).scalar_one_or_none()
    if row is None:
        return "unavailable API connection"
    return f"API GET · {row.label} · {(row.metadata_json or {}).get('base_url', '')}"


async def _authorize_actor(db, tenant_id, actor_id):
    from app.core.dependencies import has_permission
    from app.models.tenant import Tenant
    from app.models.user import User

    if actor_id is None:
        return False
    actor = (
        await db.execute(
            select(User.id)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.id == actor_id,
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.actor_type == "user",
                Tenant.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return actor is not None and await has_permission(db, actor_id, "connections.view")


def _redact(value, token):
    changed = False

    def walk(item):
        nonlocal changed
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                safe_key = walk(key)
                if key.lower() in _SECRET_KEYS:
                    result[safe_key] = "[redacted]"
                    changed = True
                else:
                    result[safe_key] = walk(child)
            return result
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, str) and token and token in item:
            changed = True
            return item.replace(token, "[redacted]")
        return item

    cleaned = walk(value)
    return cleaned, changed


async def execute(connection_id, params, tenant_id, actor_id, db, *, human_approved=False):
    if not human_approved:
        return {"error": "API requests require approval of the exact endpoint.", "hitl_required": True}
    if not isinstance(params, dict) or set(params) != {"path"}:
        return {"error": "invalid_parameters"}
    try:
        http.validate_path(params["path"], allow_query=True)
    except (ValueError, TypeError):
        return {"error": "invalid_parameters"}
    try:
        async with asyncio.timeout(30):
            if not await _authorize_actor(db, tenant_id, actor_id):
                return {"error": "permission_denied"}
            row = (await db.execute(_query(tenant_id).where(Connection.id == connection_id))).scalar_one_or_none()
            if row is None:
                return {"error": "connection_unavailable"}
            credentials = http.validate_credentials(row.provider, decrypt_credentials(row.encrypted_credentials))
            body = await http.read_json(credentials, params["path"])
            body, redacted = _redact(body, credentials.get("token"))
            if len(json.dumps(body, default=str)) > _MAX_RESULT_CHARS:
                return {
                    "error": "response_too_large_for_chat",
                    "instruction": "Use a documented filter or smaller page. No partial result was returned.",
                }
            return {"success": True, "connection_id": str(row.id), "data": body, "redacted": redacted}
    except http.ConnectorReadError as exc:
        return {"error": exc.code}
    except (TimeoutError, InvalidToken, ValueError, TypeError, RecursionError):
        return {"error": "api_request_failed"}
