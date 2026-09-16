"""Order-level intent and progress shared by chat, groups and future scheduled work.

A plan describes required work; only each signed, human-approved correction can
execute. Dependent changes are prepared from fresh evidence after the preceding
correction is verified, never from a fabricated future native snapshot.
"""

import hashlib
import json

from app.services.transaction_ops.treatments import (
    DEPENDENT_KINDS,
    KINDS,
    treatment_of,
)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    ).hexdigest()


def rules_fingerprint(proposal):
    if proposal.get("execution_transport") == "mcp_record_api":
        return fingerprint(
            {
                k: proposal.get(k)
                for k in (
                    "scope",
                    "config_id",
                    "connector_id",
                    "connector_schema",
                    "resolution_scope",
                    "accounting_book",
                    "ar_account",
                    "tax_account",
                    "sales_adjustment_account",
                )
            }
        )
    if treatment_of(proposal).family == "amendment":
        return fingerprint(
            {
                "scope": proposal["scope"],
                "config_id": proposal["config_id"],
                "native_profile": proposal["native_profile"],
            }
        )
    return fingerprint(
        {
            key: proposal.get(key)
            for key in (
                "config_id",
                "scope",
                "profile",
                "accounting_book",
                "ar_account",
                "sales_adjustment_account",
                "tax_account",
                "tax_agency",
                "period",
            )
        }
    )


def source_basis(proposal):
    source = proposal.get("source") or {}
    if treatment_of(proposal).family == "amendment":
        # New treatments bind keyed lines, revision dates and jurisdiction too.
        # Do not change the existing recipes' historical operation identities.
        return source
    return {
        key: source.get(key)
        for key in (
            "id",
            "number",
            "currency",
            "total",
            "item_total",
            "shipment_total",
            "payment_total",
            "additional_tax_total",
            "included_tax_total",
            "adjustment_total",
            "adjustments",
        )
    }


def operation_identity(proposal):
    """Same financial intent stays the same across chats and observation times."""
    if not proposal or (proposal.get("kind") or "invoice_tax") not in KINDS:
        raise ValueError("unsupported_accounting_operation")
    return fingerprint(
        {
            "version": 1,
            "tenant_id": proposal["tenant_id"],
            "scope": proposal["scope"],
            "order_reference": proposal["order_reference"],
            "kind": proposal.get("kind") or "invoice_tax",
            "record_type": proposal["record_type"],
            "record_id": proposal.get("record_id"),
            "proposed_fields": proposal["proposed_fields"],
            "expected_after": proposal["expected_after"],
            "source_basis": source_basis(proposal),
            "rules_fingerprint": rules_fingerprint(proposal),
        }
    )


def proposal_plan(proposal, report):
    kind = proposal.get("kind") or "invoice_tax"
    if kind not in KINDS:
        raise ValueError("unsupported_accounting_plan")
    order_step = kind in DEPENDENT_KINDS
    before = proposal["before"]
    order_id = (
        proposal["record_id"]
        if order_step
        else proposal.get("sales_order_id") or (before.get("createdFrom") or {}).get("id")
    )
    invoice_id = (
        proposal.get("invoice_id") if order_step or kind == "credit_tax_reallocation" else proposal.get("record_id")
    )
    amounts = (report.get("balance") or {}).get("amounts") or {}
    total = amounts.get("order_total") or {}
    source = proposal.get("source") or {}
    rules = rules_fingerprint(proposal)
    plan_id = fingerprint(
        {
            "tenant_id": proposal["tenant_id"],
            "case_id": proposal["case_id"],
            "scope": proposal["scope"],
            "source_basis": source_basis(proposal),
        }
    )
    posting = {
        "id": "posting",
        "title": "Verify posting records" if order_step else KINDS[kind],
        "status": "verified" if order_step else "awaiting_approval",
        "depends_on": [],
        "record_type": "invoice" if order_step else proposal["record_type"],
        "record_id": None if kind == "sales_adjustment_credit" else invoice_id,
        "related_invoice_id": invoice_id,
        "affects_gl": not order_step,
        "current_total": (proposal.get("support", {}).get("invoice") or before).get("total"),
        "target_total": source.get("total"),
        "evidence_basis": "verified_corrected_invoice" if order_step else "exact_correction_proposal",
    }
    sales_order = {
        "id": "sales_order",
        "title": "Align sales order with source" if order_step else "Verify sales-order consistency",
        "status": "awaiting_approval" if order_step else "waiting",
        "depends_on": ["posting"],
        "record_type": "salesorder",
        "record_id": order_id,
        "affects_gl": False,
        "current_total": before.get("total") if order_step else total.get("target"),
        "target_total": source.get("total"),
        "note": "Exact amendment requires its own approval."
        if order_step
        else "After posting verification, read the sales order again and prepare an exact amendment if needed.",
    }
    if kind == "credit_tax_reallocation":
        posting.update(
            record_id=proposal["record_id"],
            current_total=before.get("total"),
            target_total=proposal["expected_after"]["total"],
        )
    elif kind == "sales_order_line_alignment":
        predecessor = proposal["posting_predecessor"]["proposal"]
        posting.update(
            record_type="creditmemo",
            record_id=predecessor["record_id"],
            current_total=predecessor["expected_after"]["total"],
            target_total=predecessor["expected_after"]["total"],
            evidence_basis="verified_existing_credit_tax_allocation",
        )
    restriction = proposal.get("resolution_scope")
    protected = (restriction or {}).get("preserve_records") or []
    if proposal.get("execution_transport") == "mcp_record_api":
        protected = [
            {"record_type": "invoice", "record_id": invoice_id},
            {"record_type": "salesorder", "record_id": order_id},
        ]
    preserve_only = bool(protected and not order_step)
    dependent = (
        {
            "id": "preserved_records",
            "title": "Verify protected records remain unchanged",
            "status": "waiting",
            "depends_on": ["posting"],
            "read_only": True,
            "records": protected,
            "note": "This correction preserves these records. "
            "A separate amendment needs independent evidence and approval.",
        }
        if preserve_only
        else sales_order
    )
    return {
        "version": 1,
        "plan_id": plan_id,
        "case_id": proposal["case_id"],
        "order_reference": proposal["order_reference"],
        "scope": proposal["scope"],
        "rules_fingerprint": rules,
        "operation_key": operation_identity(proposal),
        "status": "awaiting_approval",
        "active_step": "sales_order" if order_step else "posting",
        "currency": source.get("currency"),
        "source_total": source.get("total"),
        "approval": {"mode": "human", "policy_id": None, "automatic_approval_enabled": False},
        "steps": [
            posting,
            dependent,
            {
                "id": "reconcile",
                "title": "Reconcile the complete order",
                "status": "waiting",
                "depends_on": ["posting", dependent["id"]],
                "verification_scope": "order_total_tax_refunds",
            },
        ],
        "cash_settlement": "separate_verification_required",
        "next_action": "Review the exact correction. Later dependent changes require fresh evidence and approval.",
    }


def completed_plan(proposal, report, status, next_step):
    plan = proposal_plan(proposal, report)
    plan.update(status=status, next_action=next_step)
    for step in plan["steps"]:
        if status == "reconciled":
            step["status"] = "verified"
        elif step["id"] == plan["active_step"]:
            step["status"] = "verified" if status == "partially_resolved" else "needs_review"
        elif step["id"] == "reconcile":
            step["status"] = "needs_review"
        elif step["id"] == "sales_order":
            step["status"] = "awaiting_approval" if next_step.get("kind") in DEPENDENT_KINDS else "waiting"
    return plan


async def previous_execution(db, tenant_id, message_id, proposal):
    """Called while the existing account/invoice lock is held, before a new CAS.

    A timed-out or unknown result blocks another send. Only a recorded failure
    of preconditions with zero writes, or an adapter proof of a rejected update
    with a freshly unchanged subledger, can relinquish the same business intent.
    """
    from sqlalchemy import select

    from app.models.audit import AuditEvent
    from app.models.chat import ChatMessage

    if str(tenant_id) != proposal["tenant_id"]:
        raise ValueError("accounting_operation_tenant_mismatch")
    key = operation_identity(proposal)
    from sqlalchemy import String, and_, cast, exists, not_

    so = ChatMessage.structured_output
    safe_precondition_failure = exists(
        select(AuditEvent.id).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.resource_id == cast(ChatMessage.id, String),
            AuditEvent.action == "accounting_correction.precondition_failed",
            AuditEvent.payload["approved_by"].astext == so["accounting_execution"]["approved_by"].astext,
            AuditEvent.payload["financial_writes"].astext == "0",
        )
    )
    query = (
        select(ChatMessage)
        .where(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.id != message_id,
            so["accounting_execution"]["operation_key"].astext == key,
            not_(and_(so["status"].astext == "failed", safe_precondition_failure)),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(1)
    )
    from app.services.audit_service import log_event
    from app.services.transaction_ops.accounting_retry import rejected_credit_unchanged

    # Inspect every prior attempt, so a rejected newest attempt cannot conceal
    # an older successful/uncertain one. Fail closed on unusually long histories.
    released = []
    for attempt in range(20):
        message = await db.scalar(query.where(ChatMessage.id.not_in(released)))
        if message is None:
            return None
        proof = await rejected_credit_unchanged(db, tenant_id, message, proposal) if attempt < 19 else None
        if proof:
            await log_event(
                db,
                tenant_id,
                "transaction_ops",
                "accounting_correction.rejection_verified_unchanged",
                actor_type="system",
                resource_type="chat_message",
                resource_id=str(message.id),
                payload={**proof, "replacement_confirmation_id": str(message_id), "operation_key": key},
            )
            released.append(message.id)
            continue
        so = message.structured_output
        return {
            "confirmation_id": str(message.id),
            "session_id": str(message.session_id),
            "status": so.get("status"),
            "verification": so.get("accounting_verification"),
            "operation_key": key,
        }
    return None
