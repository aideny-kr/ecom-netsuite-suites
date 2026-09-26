"""Durable per-call audit for external MCP, independent of the chat transaction."""

import asyncio
import hashlib
import json
import uuid

from app.core.database import async_session_factory, set_tenant_context
from app.services.audit_service import log_event


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if any(s in k.lower() for s in ("token", "secret", "password", "authorization", "api_key"))
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(redact(json.loads(value)))
        except (ValueError, RecursionError):
            pass
    return value


class ExternalCallNotSentError(RuntimeError):
    """The request could not be recorded, so the external system was never called.

    A caller that holds a one-use send permit can end its attempt as refused before effect
    instead of unknown: nothing left this process, which a later readback could never prove.
    """


def session_factory_for(db):
    """The session factory a caller prepared for this event loop, if it prepared one.

    A Celery task runs in its own event loop. The app-wide pool keeps connections opened by an
    earlier task's loop, and touching one fails with "Event loop is closed" (six times during
    one group dispatch on 2026-09-26). The group dispatcher already prepares a factory on its
    own engine for exactly this (accounting_dispatch); the audit rows use it too.
    """
    info = getattr(db, "info", None)
    return info.get("accounting_authorization_session_factory") if isinstance(info, dict) else None


async def append_event(*, session_factory=None, **kwargs):
    async with (session_factory or async_session_factory)() as audit_db:
        await set_tenant_context(audit_db, str(kwargs["tenant_id"]))
        await log_event(db=audit_db, **kwargs)
        await audit_db.commit()


async def audited_external_call(
    *,
    execute,
    tenant_id,
    actor_id,
    actor_type,
    correlation_id,
    session_id,
    connector_id,
    tool_name,
    params,
    human_approved,
    approval_context=None,
    session_factory=None,
):
    call_id = str(uuid.uuid4())
    common = dict(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type=actor_type,
        correlation_id=correlation_id,
        category="tool_call",
        resource_type="external_mcp_tool",
        resource_id=tool_name,
    )
    payload = {
        "call_id": call_id,
        "session_id": session_id,
        "connector_id": str(connector_id),
        "tool_name": tool_name,
        "params": redact(params),
        "human_approved": human_approved,
        "approved_by": str(actor_id) if human_approved and actor_id else None,
        "approval": approval_context if human_approved else None,
    }
    # Fail closed before invoking an external system if the durable request cannot be recorded.
    try:
        await append_event(
            **common, action="tool.requested", payload=payload, status="pending", session_factory=session_factory
        )
    except Exception as exc:
        raise ExternalCallNotSentError("request_audit_unavailable") from exc
    try:
        result = await execute()
    except asyncio.CancelledError:
        await asyncio.shield(
            append_event(
                **common,
                action="tool.interrupted",
                payload=payload,
                status="unknown",
                error_message="Cancelled while awaiting external response; request may have reached provider.",
                session_factory=session_factory,
            )
        )
        raise
    except Exception:
        await append_event(
            **common,
            action="tool.failed",
            payload=payload,
            status="error",
            error_message="External execution raised; consult correlated application log.",
            session_factory=session_factory,
        )
        raise
    body = result if isinstance(result, dict) else {}
    failed = bool(body.get("error")) or body.get("isError") is True or body.get("success") is False
    # The cache's age marker is provenance, not content: identical provider data hashes
    # alike whether it came live or from the dispatcher's metadata cache.
    content = {k: v for k, v in body.items() if k != "served_from_cache"} if isinstance(result, dict) else result
    encoded = json.dumps(content, sort_keys=True, default=str).encode()
    payload = {
        **payload,
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
        "result_bytes": len(encoded),
        "connection_scope": body.get("verified_connection_scope"),
        # Set only by the dispatcher's metadata cache: no round trip reached the provider.
        "served_from_cache": body.get("served_from_cache"),
    }
    try:
        await append_event(
            **common,
            action="tool.failed" if failed else "tool.executed",
            payload=payload,
            status="error" if failed else "success",
            session_factory=session_factory,
        )
    except Exception:
        # Do not turn a completed write into a retryable transport error.
        result = {
            **(result if isinstance(result, dict) else {"result": result}),
            "audit_completion_pending": True,
            "audit_call_id": call_id,
            "instruction": "Do not retry this call. Audit completion needs repair.",
        }
    return result
