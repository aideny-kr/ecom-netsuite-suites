"""Durable, bounded publication and next-step preparation after reconciliation.

This worker never executes a financial tool. It uses the original audited
approval to describe the result, and fresh authorized reads to prepare a new
human-approval card when another supported correction is required.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import Integer, cast, select

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionCase, TransactionFinding, TransactionRun
from app.models.user import User
from app.services.audit_service import log_event
from app.services.transaction_ops.accounting_history import _claim, verified_resolution
from app.services.transaction_ops.accounting_recheck import effective_config_id
from app.services.transaction_ops.resolution_plan import completed_plan, operation_identity
from app.services.transaction_ops.treatments import treatment_or_none

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=3)
BUSY_RETRY_DELAY = timedelta(seconds=60)  # the slot, not the work, was the problem


def enqueue(message, run, now):
    if not _claim(message):
        return  # Legacy unauthenticated outcomes cannot manufacture a success receipt.
    so = message.structured_output
    previous = so.get("accounting_completion") or {}
    if previous.get("run_id") == str(run.id):
        return
    message.structured_output = {
        **so,
        "accounting_completion": {
            "version": 1,
            "run_id": str(run.id),
            "status": "pending",
            "attempts": 0,
            "next_at": now.isoformat(),
        },
    }


async def candidates(db, tenant_id, now, *, limit):
    await set_tenant_context(db, str(tenant_id))
    work = ChatMessage.structured_output["accounting_completion"]
    return list(
        await db.scalars(
            select(ChatMessage.id)
            .where(
                ChatMessage.tenant_id == tenant_id,
                work["version"].astext == "1",
                work["status"].astext.in_(("pending", "running")),
                cast(work["attempts"].astext, Integer) <= MAX_ATTEMPTS,
                work["next_at"].astext <= now.isoformat(),
            )
            .order_by(work["next_at"].astext, ChatMessage.id)
            .limit(limit)
        )
    )


async def _message(db, tenant_id, message_id):
    return await db.scalar(
        select(ChatMessage)
        .where(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.id == message_id,
        )
        .execution_options(populate_existing=True)
    )


def eligible(so, now):
    work = so.get("accounting_completion") or {}
    try:
        return (
            work["version"] == 1
            and work["status"] in {"pending", "running"}
            and (
                0 <= work["attempts"] < MAX_ATTEMPTS or work["attempts"] == MAX_ATTEMPTS and work["status"] == "running"
            )
            and datetime.fromisoformat(work["next_at"]) <= now
        )
    except (KeyError, TypeError, ValueError):
        return False


async def _evidence(db, tenant_id, message):
    from app.services.transaction_ops.accounting_recheck import report_in_scope

    so = message.structured_output
    p, claim = so["accounting_review"], _claim(message)
    if not claim or p.get("tenant_id") != str(tenant_id):
        raise ValueError("completion_approval_unverified")
    audit = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.resource_id == str(message.id),
            AuditEvent.action == "accounting_correction.approval_claimed",
            AuditEvent.actor_id == UUID(claim["approved_by"]),
            AuditEvent.payload["evidence_digest"].astext == claim["evidence_digest"],
            AuditEvent.payload["accepted_at"].astext == claim["accepted_at"],
        )
        .limit(1)
    )
    run = await db.get(TransactionRun, UUID(so["accounting_completion"]["run_id"]))
    if not audit or not run or run.tenant_id != tenant_id:
        raise ValueError("completion_provenance_unverified")
    result = (run.progress_json or {}).get("settlement") or {}
    case = await db.get(TransactionCase, UUID(p["case_id"]))
    finding = await db.get(TransactionFinding, UUID(result["finding_id"])) if result.get("finding_id") else None
    from app.services.transaction_ops.settlement import SCOPE
    from app.services.transaction_ops.state_service import business_digest

    if (
        run.status != "finished"
        or run.params_json.get("approval_message_id") != str(message.id)
        or run.params_json.get("approved_by") != claim["approved_by"]
        or result.get("approval_message_id") != str(message.id)
        or result.get("approved_by") != claim["approved_by"]
        or result.get("case_id") != p["case_id"]
        or run.origin != "recovery"
        or str(run.config_id) != str(effective_config_id(so) or "")
        or run.work_key != business_digest({"accounting_recheck_confirmation": str(message.id)})
        or run.params_json.get("order_references") != [p["order_reference"]]
        or run.params_json.get("verification_scope") != SCOPE
        or result.get("verification_scope") != SCOPE
        or str(run.initiated_by) != claim["approved_by"]
        or not case
        or case.tenant_id != tenant_id
        or case.scope_json != p["scope"]
        or case.order_reference != p["order_reference"]
        or (
            finding
            and (
                finding.tenant_id != tenant_id
                or finding.run_id != run.id
                or finding.order_reference != p["order_reference"]
            )
        )
    ):
        raise ValueError("completion_scope_unverified")
    verified = bool(finding and verified_resolution(message, run, finding, case))
    report = finding.report_json if finding else {}
    in_scope = bool(
        finding
        and run.termination_reason == "done"
        and result.get("termination_reason") == "done"
        and report_in_scope(run, p, report, run.finished_at)
    )
    user = await db.scalar(select(User).where(User.id == UUID(claim["approved_by"]), User.tenant_id == tenant_id))
    return p, claim, audit, run, case, report, verified, in_scope, user


def record_links(p, verification, report):
    from app.services.transaction_ops.record_links import evidence_record_links

    order = verification.get("sales_order") or {}
    if not order and len(report.get("targets") or []) == 1:
        order = {"id": report["targets"][0].get("record_id"), "tranId": p["order_reference"]}
    documents = []
    invoice = verification.get("invoice") or (p.get("support") or {}).get("invoice")
    # A correction whose reconciliation target is its own record is the sales order
    # itself; every other treatment's "before" document (and a foreign kind's, as
    # before this registry) is the invoice it corrected.
    row = treatment_or_none(p)
    if not invoice and (row is None or row.reconciliation_target != "record"):
        invoice = {**p["before"], "id": p["record_id"]}
    if invoice:
        documents.append({**invoice, "record_type": "invoice"})
    if verification.get("credit_memo_id"):
        documents.append({"id": verification["credit_memo_id"], "record_type": "creditmemo"})
    links = evidence_record_links(
        {
            "verified_connection_scope": {"account_id": p["scope"]["netsuite_account_id"]},
            "sections": {"sales_order": {**order, "record_type": "salesorder"}, "posting_documents": documents},
        }
    )
    for link in links:
        link["label"] = {"salesorder": "Sales order", "invoice": "Invoice", "creditmemo": "Credit memo"}[
            link["record_type"]
        ]
    return links


async def prepare_next(db, tenant_id, message, actor_id):
    from app.mcp.tools.transaction_ops_tools import execute_accounting_evidence
    from app.services.chat.tools import build_all_tool_definitions
    from app.services.policy_service import get_active_policy
    from app.services.transaction_ops.tax_correction import candidate_confirmation

    p = message.structured_output["accounting_review"]
    from app.services.transaction_ops.case_resolution_scope import load

    restriction = await load(db, tenant_id, p["case_id"])
    if restriction and restriction["allowed_mutations"] == [
        {k: p.get(k) for k in ("kind", "record_type", "record_id")}
    ]:
        return None, {
            "status": "scope_complete",
            "reasons": [
                "No additional record amendments are authorized by this case scope. "
                "Verify the correction and preserve the invoice and sales order."
            ],
        }
    if p.get("execution_transport") == "mcp_record_api":
        return None, {
            "status": "independent_review_required",
            "reasons": [
                "The credit correction does not establish an error in the original invoice or sales order. "
                "Investigate any remaining discrepancy independently before proposing another amendment."
            ],
        }
    db.info.pop("accounting_correction_candidate", None)
    result = await execute_accounting_evidence(
        {"case_id": p["case_id"]},
        context={
            "db": db,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "session_id": message.session_id,
            "correlation_id": "accounting-next:" + str(message.id),
        },
    )
    candidate = db.info.get("accounting_correction_candidate")
    evidence = result.get("accounting_evidence") or {}
    if not result.get("success") or not candidate:
        return None, {
            "status": "blocked",
            "reasons": evidence.get("blockers") or ["No supported exact correction is ready."],
        }
    if operation_identity(candidate) == operation_identity(p):
        return None, {
            "status": "blocked",
            "reasons": ["The previous correction is already recorded; its write will not be repeated."],
        }
    if any(candidate.get(key) != p.get(key) for key in ("tenant_id", "case_id", "scope", "order_reference")):
        return None, {"status": "blocked", "reasons": ["The next correction belongs to a different accounting scope."]}
    from app.services.transaction_ops.resolution_plan import previous_execution

    prior = await previous_execution(db, tenant_id, message.id, candidate)
    if prior:
        return None, {
            "status": "blocked",
            "reasons": ["This next correction already has a recorded execution."],
            "existing_confirmation_id": prior["confirmation_id"],
        }
    existing = await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.structured_output["status"].astext == "pending",
            ChatMessage.structured_output["accounting_review"]["resolution_plan"]["operation_key"].astext
            == operation_identity(candidate),
        )
        .limit(1)
    )
    if existing:
        return None, {
            "status": "blocked",
            "reasons": ["An exact approval for this next correction is already pending."],
            "existing_confirmation_id": str(existing),
        }
    prepared = await candidate_confirmation(
        db=db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id="accounting-next:" + str(message.id),
        session_id=str(message.session_id),
        task="Prepare the next exact correction in this order resolution plan for human approval",
        tools=await build_all_tool_definitions(db, tenant_id),
        policy=await get_active_policy(db, tenant_id),
        case_id=p["case_id"],
    )
    if not prepared:
        return None, {"status": "blocked", "reasons": ["The next correction could not pass approval preparation."]}
    card, note = prepared
    if card.status != "pending" or card.invariant_errors or card.editable_slots or card.unfillable_line_fields:
        return None, {
            "status": "blocked",
            "reasons": card.invariant_errors or ["The next correction needs additional evidence."],
        }
    child_id = uuid4()
    child = ChatMessage(
        id=child_id,
        tenant_id=tenant_id,
        session_id=message.session_id,
        role="assistant",
        content=note,
        structured_output={**card.model_dump(mode="json"), "accounting_plan_predecessor": str(message.id)},
        token_count=0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
    )
    return child, {
        "status": "awaiting_approval",
        "confirmation_id": str(child_id),
        "kind": candidate.get("kind") or "invoice_tax",
        "operation_key": operation_identity(candidate),
    }


def narrative(receipt):
    def plain(value):
        import re

        return re.sub(r"([\\`*_{}\[\]()#+.!|<>])", r"\\\1", str(value)).replace("\n", " ")

    title = {"reconciled": "Reconciled", "partially_resolved": "Correction verified; further work remains"}.get(
        receipt["status"], "Verification needs review"
    )
    currency = (receipt.get("plan") or {}).get("currency")
    lines = [f"**{title} · {plain(receipt['order_reference'])}**", receipt["summary"]]
    if currency:
        lines.append(f"Amounts in {plain(currency)} · variance = source − ERP.")
    amounts = (receipt.get("balance") or {}).get("amounts") or {}
    if amounts:
        table = ["| Comparison | Source | ERP | Variance |", "| --- | ---: | ---: | ---: |"]
        for metric, label in (("order_total", "Order total"), ("tax", "VAT / tax"), ("refunds", "Refunds")):
            values = amounts.get(metric) or {}
            table.append(
                "| "
                + " | ".join(
                    [
                        label,
                        *[
                            plain(values.get(k)) if values.get(k) is not None else "—"
                            for k in ("source", "target", "delta")
                        ],
                    ]
                )
                + " |"
            )
        lines += ["\n".join(table)]
    links = [f"[{link['label']}]({link['url']})" for link in receipt["record_links"]]
    links += [f"[Reconciliation result]({receipt['reconciliation_url']})", "[Audit log](/audit)"]
    lines += [
        " · ".join(links),
        f"Approved by {plain(receipt['approved_by']['name'])} · {receipt['approved_at']}. "
        "The exact changes, approval and verification are retained in the audit log. Cash settlement remains separate.",
    ]
    return "\n\n".join(lines)


async def complete(db, tenant_id, message_id, *, now=None, lock_engine=None):
    """Publish once under the same cross-process invoice lock as the writer.

    Only the retry marker is committed before work. Receipt, fresh pending card
    and their audit are committed together; a killed worker can safely resume.
    """
    from app.services.transaction_ops.accounting_group import accounting_write_slot
    from app.services.transaction_ops.accounting_recovery import _authorize_read, group_of, refresh_group

    now = now or datetime.now(timezone.utc)
    await set_tenant_context(db, str(tenant_id))
    message = await _message(db, tenant_id, message_id)
    if not message or not eligible(message.structured_output or {}, now):
        return {"status": "not_due", "financial_writes": 0}
    claimed = False
    try:
        async with accounting_write_slot(message.structured_output["accounting_review"], lock_engine=lock_engine):
            message = await _message(db, tenant_id, message_id)
            if not message or not eligible(message.structured_output or {}, now):
                return {"status": "not_due", "financial_writes": 0}
            if message.structured_output["accounting_completion"]["attempts"] >= MAX_ATTEMPTS:
                await defer(db, tenant_id, message, now, "worker_interrupted")
                await db.commit()
                return {"status": "blocked", "financial_writes": 0}
            work = {
                **message.structured_output["accounting_completion"],
                "status": "running",
                "attempts": message.structured_output["accounting_completion"]["attempts"] + 1,
                "next_at": (now + RETRY_DELAY).isoformat(),
            }
            message.structured_output = {**message.structured_output, "accounting_completion": work}
            await db.commit()
            claimed = True
            await set_tenant_context(db, str(tenant_id))
            async with asyncio.timeout(150):
                message = await _message(db, tenant_id, message_id)
                p, claim, audit, run, case, report, verified, in_scope, user = await _evidence(db, tenant_id, message)
                actor = await _authorize_read(db, tenant_id, message, claim)
                native = message.structured_output.get("accounting_verification") or {}
                native_verified = (
                    message.structured_output.get("status") == "approved" and native.get("status") == "verified"
                )
                next_card = None
                next_step = (
                    {"status": "complete"}
                    if verified
                    else {
                        "status": "blocked",
                        "reasons": ["Full reconciliation is not verified. Review the remaining evidence."],
                    }
                )
                if (
                    not verified
                    and native_verified
                    and in_scope
                    and report.get("balance", {}).get("status") == "difference"
                ):
                    try:
                        async with db.begin_nested(), asyncio.timeout(120):
                            next_card, next_step = await prepare_next(db, tenant_id, message, actor)
                    except Exception as exc:
                        next_step = {
                            "status": "blocked",
                            "reasons": [
                                "The correction remains verified. Preparing the next exact approval "
                                "needs further investigation."
                            ],
                        }
                        await log_event(
                            db,
                            tenant_id,
                            "transaction_ops",
                            "accounting_plan.next_step_unavailable",
                            actor_type="system",
                            resource_type="chat_message",
                            resource_id=str(message.id),
                            payload={"reason": type(exc).__name__, "financial_writes": 0},
                            status="error",
                        )
                status = "reconciled" if verified else "partially_resolved" if native_verified else "needs_review"
                receipt_id = uuid4()
                receipt = {
                    "version": 1,
                    "status": status,
                    "order_reference": p["order_reference"],
                    "case_id": p["case_id"],
                    "plan": completed_plan(p, report, status, next_step),
                    "confirmation_id": str(message.id),
                    "completion_message_id": str(receipt_id),
                    "approval_audit_id": str(audit.id),
                    "approved_at": claim["accepted_at"],
                    "approved_by": {"id": str(actor), "name": user.full_name if user else str(actor)},
                    "native_verification": native.get("status", "not_verified"),
                    "reconciliation_run_id": str(run.id),
                    "reconciliation_status": "succeeded" if verified else "not_verified",
                    "reconciliation_url": f"/transaction-operations/runs/{run.id}",
                    "audit_url": "/audit",
                    "record_links": record_links(p, native, report),
                    "before": p["before"],
                    "verified_after": native,
                    "balance": report.get("balance") or {},
                    "next_step": next_step,
                    "cash_settlement": "not_verified",
                    "checked_at": run.progress_json["settlement"]["checked_at"],
                    "published_at": now.isoformat(),
                    "summary": "The correction is verified and source, sales-order totals, tax and refunds reconcile."
                    if verified
                    else "The native correction is verified. The complete order still needs review."
                    if native_verified
                    else "The complete correction and reconciliation could not be verified.",
                }
                group_id = group_of(claim)
                if next_card:
                    # Explicit order survives equal transaction timestamps and UUID sorting.
                    next_card.created_at = now + timedelta(microseconds=1)
                    if group_id:
                        next_card.structured_output = {**next_card.structured_output, "accounting_group_child": True}
                    db.add(next_card)
                if group_id:
                    receipt["completion_message_id"] = None
                event = await log_event(
                    db,
                    tenant_id,
                    "transaction_ops",
                    "accounting_plan.completion",
                    actor_id=actor,
                    actor_type="system",
                    resource_type="chat_message",
                    resource_id=str(message.id),
                    payload={**receipt, "financial_writes": 0},
                )
                receipt["completion_audit_id"] = str(event.id)
                if not group_id:
                    db.add(
                        ChatMessage(
                            id=receipt_id,
                            tenant_id=tenant_id,
                            session_id=message.session_id,
                            role="assistant",
                            content=narrative(receipt),
                            structured_output={"type": "accounting_receipt", "accounting_receipt": receipt},
                            token_count=0,
                            input_tokens=0,
                            output_tokens=0,
                            cache_creation_tokens=0,
                            cache_read_tokens=0,
                            created_at=now,
                        )
                    )
                message.structured_output = {
                    **message.structured_output,
                    "accounting_receipt": receipt,
                    "accounting_completion": {**work, "status": "done", "finished_at": now.isoformat()},
                }
                await db.flush()
                if group_id:
                    await refresh_group(db, tenant_id, message.session_id, group_id)
                await db.commit()
                return {"status": status, "financial_writes": 0, "next_approval": next_step.get("confirmation_id")}
    except Exception as exc:
        await db.rollback()
        if not claimed:
            # The per-account write slot was busy: nothing was claimed, so the retry marker
            # still says whatever it said. Pull it to the next collector tick, or the
            # receipt waits out a stale next_at behind a slot that is free again.
            await set_tenant_context(db, str(tenant_id))
            message = await _message(db, tenant_id, message_id)
            work = (message.structured_output or {}).get("accounting_completion") if message else None
            if work and work.get("status") == "pending":
                message.structured_output = {
                    **message.structured_output,
                    "accounting_completion": {**work, "next_at": (now + BUSY_RETRY_DELAY).isoformat()},
                }
                await db.commit()
            return {"status": "busy", "financial_writes": 0}
        await set_tenant_context(db, str(tenant_id))
        message = await _message(db, tenant_id, message_id)
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("completion_") else type(exc).__name__
        status = await defer(db, tenant_id, message, now, reason)
        await db.commit()
        return {"status": status, "financial_writes": 0}


async def defer(db, tenant_id, message, now, reason):
    work = message.structured_output["accounting_completion"]
    status = "blocked" if work["attempts"] >= MAX_ATTEMPTS else "pending"
    message.structured_output = {
        **message.structured_output,
        "accounting_completion": {
            **work,
            "status": status,
            "reason": reason,
            "next_at": (now + RETRY_DELAY).isoformat(),
        },
    }
    await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting_plan.completion_deferred",
        actor_type="system",
        resource_type="chat_message",
        resource_id=str(message.id),
        payload={"status": status, "reason": reason, "financial_writes": 0},
        status="error",
    )
    if status == "blocked":
        from app.services.transaction_ops.accounting_recovery import group_of, refresh_group

        group_id = group_of(_claim(message))
        if group_id:
            await refresh_group(db, tenant_id, message.session_id, group_id)
        else:
            db.add(
                ChatMessage(
                    tenant_id=tenant_id,
                    session_id=message.session_id,
                    role="assistant",
                    content="The accounting result needs review: automatic completion checks could not finish. "
                    "The recorded financial operation has not been retried. "
                    "Review [Transactions](/transaction-operations) "
                    "and the [audit log](/audit) for the original approval and verification evidence.",
                    structured_output={"type": "accounting_completion_blocked", "confirmation_id": str(message.id)},
                    token_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                )
            )
    return status
