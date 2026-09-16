"""One send for one durably claimed, signed native accounting approval."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, text

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.services.audit_service import log_event
from app.services.transaction_ops import native_accounting_service as service
from app.services.transaction_ops import native_accounting_transport as transport

RESERVED = "accounting.native_dispatch.reserved"


def _duplicate(previous):
    return {
        "success": False,
        "status": "outcome_unconfirmed",
        "retry_allowed": False,
        "financial_writes": None,
        "outcome_indeterminate": True,
        "reason": "prior_native_send_reservation_requires_read_only_recovery",
        "reservation_audit_id": str(previous),
    }


async def _previous(db, tenant_id, key):
    return await db.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == RESERVED,
            AuditEvent.payload["operation_key"].astext == key,
        )
        .limit(1)
    )


async def execute(db, tenant_id, actor_id, session_id, params, approval_context, *, correlation_id=None):
    from app.services.transaction_ops.accounting_group import authorize_accounting_write
    from app.services.transaction_ops.accounting_recovery import _authorize_read, _message, evidence_digest
    from app.services.transaction_ops.resolution_plan import operation_identity

    await set_tenant_context(db, str(tenant_id))
    confirmation_id = UUID((approval_context or {})["confirmation_id"])
    message = await _message(db, tenant_id, confirmation_id)
    so = message.structured_output if message else {}
    claim, proposal = so.get("accounting_execution") or {}, so.get("accounting_review") or {}
    if (
        not message
        or str(message.session_id) != str(session_id)
        or so.get("status") != "executing"
        or claim.get("approved_by") != str(actor_id)
        or claim.get("approval_context") != approval_context
        or so.get("tool_name") != service.TOOL
        or so.get("tool_input") != params
    ):
        raise ValueError("native_durable_approval_required")
    approved_actor = await _authorize_read(db, tenant_id, message, claim)
    if str(approved_actor) != str(actor_id):
        raise ValueError("native_approval_actor_mismatch")
    service.validate_binding(tenant_id, so["tool_name"], params, proposal)
    await authorize_accounting_write(db, tenant_id, actor_id, service.TOOL, params)
    key = operation_identity(proposal)
    if claim.get("operation_key") != key:
        raise ValueError("native_operation_identity_mismatch")
    previous = await _previous(db, tenant_id, key)
    if previous:
        return _duplicate(previous)
    # Fresh source, rules, linked postings and native record revision. Never use
    # a model-supplied boolean or an old in-memory preflight as send authority.
    try:
        await service.validate_approved(db, tenant_id, service.TOOL, params, proposal)
    except Exception as exc:
        previous = await _previous(db, tenant_id, key)
        if previous:
            return _duplicate(previous)
        reason = str(exc)[:240] if isinstance(exc, ValueError) else "native_preflight_unavailable"
        await log_event(
            db,
            tenant_id,
            "transaction_ops",
            "accounting.native_preflight.failed",
            actor_id=actor_id,
            resource_type="chat_message",
            resource_id=str(confirmation_id),
            correlation_id=correlation_id,
            status="error",
            payload={"operation_key": key, "approved_by": str(actor_id), "reason": reason, "financial_writes": 0},
        )
        await db.commit()
        await set_tenant_context(db, str(tenant_id))
        return {
            "success": False,
            "status": "not_submitted",
            "error": reason,
            "financial_writes": 0,
            "retry_allowed": False,
        }
    await authorize_accounting_write(db, tenant_id, actor_id, service.TOOL, params)
    await set_tenant_context(db, str(tenant_id))
    # Short operation-scoped DB lock; released with reservation commit before IO.
    lock = int(key[:16], 16)
    if lock >= 2**63:
        lock -= 2**64
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    previous = await _previous(db, tenant_id, key)
    if previous:
        await db.commit()
        await set_tenant_context(db, str(tenant_id))
        return _duplicate(previous)
    # Re-read after token refresh / DB commits and the reservation lock wait.
    current = await _message(db, tenant_id, confirmation_id)
    current_so = current.structured_output if current else {}
    if (
        current_so.get("status") != "executing"
        or current_so.get("accounting_execution") != claim
        or evidence_digest(current_so) != evidence_digest(so)
    ):
        raise ValueError("native_approval_changed_before_reservation")
    reservation = await log_event(
        db,
        tenant_id,
        "transaction_ops",
        RESERVED,
        actor_id=actor_id,
        resource_type="chat_message",
        resource_id=str(confirmation_id),
        correlation_id=correlation_id,
        payload={
            "operation_key": key,
            "approved_by": str(actor_id),
            "approval_context": approval_context,
            "evidence_digest": evidence_digest(so),
            "request": proposal["native_request"],
            "before": proposal["native_preview"]["beforeSnapshot"],
            "financial_writes": 0,
            "outcome": "reserved_before_send",
            "retry_allowed": False,
        },
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    # There is deliberately no loop/retry after this committed reservation.
    # A crash before/after HTTP is recovered by reading; it never resubmits.
    try:
        result = await transport._request(
            db,
            tenant_id,
            proposal["connection_id"],
            proposal["scope"]["netsuite_account_id"],
            "apply",
            {
                "request": proposal["native_request"],
                "expected_before": proposal["native_preview"]["beforeSnapshot"],
                "work_key": key,
                "approval_audit_id": str(reservation.id),
                "approval_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            },
        )
    except Exception:
        result = {
            "success": False,
            "status": "outcome_unconfirmed",
            "financial_writes": None,
            "retry_allowed": False,
            "reason": "native_response_unconfirmed",
        }
    confirmed = (
        result.get("success") is True
        and result.get("record_type") == proposal["record_type"]
        and result.get("record_id") == proposal["record_id"]
        and result.get("work_key") == key
        and type(result.get("financial_writes")) is int
        and result["financial_writes"] == 1
    )
    refused = (
        result.get("success") is False
        and result.get("status") == "not_submitted"
        and type(result.get("financial_writes")) is int
        and result["financial_writes"] == 0
    )
    result = {
        **result,
        "reservation_audit_id": str(reservation.id),
        "retry_allowed": False,
        "outcome_indeterminate": not confirmed and not refused,
    }
    await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting.native_dispatch.returned",
        actor_id=actor_id,
        resource_type="chat_message",
        resource_id=str(confirmation_id),
        correlation_id=correlation_id,
        payload={"operation_key": key, "approved_by": str(actor_id), "receipt": result, "verification_required": True},
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    return result
