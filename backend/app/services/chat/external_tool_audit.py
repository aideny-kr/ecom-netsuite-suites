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


async def append_event(**kwargs):
    async with async_session_factory() as audit_db:
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
    }
    # Fail closed before invoking an external system if the durable request cannot be recorded.
    await append_event(**common, action="tool.requested", payload=payload, status="pending")
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
        )
        raise
    body = result if isinstance(result, dict) else {}
    failed = bool(body.get("error")) or body.get("isError") is True or body.get("success") is False
    encoded = json.dumps(result, sort_keys=True, default=str).encode()
    payload = {
        **payload,
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
        "result_bytes": len(encoded),
        "connection_scope": body.get("verified_connection_scope"),
    }
    try:
        await append_event(
            **common,
            action="tool.failed" if failed else "tool.executed",
            payload=payload,
            status="error" if failed else "success",
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
