"""Bounded read-only recovery of a durably approved, interrupted accounting correction.

Uses the existing minute scheduler and invoice lock. Never dispatches a write,
resets an approval, or asks a model to infer whether posting succeeded.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import Integer, String, and_, cast, exists, or_, select

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.models.transaction_ops import TransactionOperation, TransactionRun
from app.services.audit_service import log_event
from app.services.transaction_ops.treatments import VERIFIED_KINDS

MAX_ATTEMPTS = 3
DELAY = timedelta(minutes=5)
CLAIM_ACTION = "accounting_correction.approval_claimed"


def evidence_digest(so):
    # Fingerprint exact persisted JSON, not a monetary business key. Native
    # source evidence includes JSON numbers; never round or calculate with them.
    value = {k: so.get(k) for k in ("tool_name", "tool_input", "accounting_review")}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def execution_claim(so, confirmation_id, actor_id, context, *, now):
    """Called only after human-approval gates, persisted with the winning CAS."""
    from app.services.transaction_ops.resolution_plan import operation_identity

    return {
        **so,
        "accounting_execution": {
            "version": 1,
            "confirmation_id": str(confirmation_id),
            "approved_by": str(actor_id),
            "accepted_at": now.isoformat(),
            "evidence_digest": evidence_digest(so),
            "operation_key": operation_identity(so["accounting_review"]),
            "approval_context": context,
            "attempts": 0,
            "next_at": (now + DELAY).isoformat(),
        },
    }


def eligible(so, now):
    from app.services.transaction_ops.accounting_recheck import supports

    p, claim = so.get("accounting_review") or {}, so.get("accounting_execution") or {}
    try:
        return (
            supports(p)
            and claim.get("version") == 1
            and so.get("status") in {"executing", "indeterminate", "approved"}
            and (so.get("accounting_recheck") or {}).get("status") != "queued"
            and 0 <= claim["attempts"] < MAX_ATTEMPTS
            and datetime.fromisoformat(claim["next_at"]) <= now
        )
    except (KeyError, ValueError, TypeError):
        return False


async def ledger_candidates(db, tenant_id, now, *, limit):
    """Cards the write kernel claimed whose attempt needs a read-only recovery: an open
    row past its deadline (its sender is gone), or an unknown one; never one a recovery
    run already finished or is running under a live lease."""
    from app.services.transaction_ops import state_service as state

    await set_tenant_context(db, str(tenant_id))
    busy_or_done = exists(
        select(TransactionRun.id).where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.origin == "recovery",
            TransactionRun.params_json["operation_id"].astext == cast(TransactionOperation.id, String),
            or_(
                TransactionRun.status == "finished",
                and_(
                    TransactionRun.status == "running",
                    TransactionRun.lease_until > now,
                    TransactionRun.deadline_at > now,
                ),
            ),
        )
    )
    return list(
        (
            await db.scalars(
                select(TransactionOperation.approval_id)
                .where(
                    TransactionOperation.tenant_id == tenant_id,
                    TransactionOperation.approval_kind == "chat_confirmation",
                    ~busy_or_done,
                    or_(
                        and_(TransactionOperation.status.in_(state.OPEN), TransactionOperation.deadline_at <= now),
                        TransactionOperation.status == "unknown",
                    ),
                )
                .order_by(TransactionOperation.attempted_at, TransactionOperation.id)
                .limit(limit)
            )
        ).all()
    )


async def candidates(db, tenant_id, now, *, limit):
    """Cards due for recovery: those the kernel claimed (the ledger says so) and, until
    the native amendment card moves onto the kernel, those carrying their own claim."""
    ledger = await ledger_candidates(db, tenant_id, now, limit=limit)
    legacy = await _legacy_candidates(db, tenant_id, now, limit=limit)
    return (ledger + [m for m in legacy if m not in ledger])[:limit]


async def _legacy_candidates(db, tenant_id, now, *, limit):
    await set_tenant_context(db, str(tenant_id))
    so = ChatMessage.structured_output
    return list(
        (
            await db.scalars(
                select(ChatMessage.id)
                .where(
                    ChatMessage.tenant_id == tenant_id,
                    or_(
                        so["accounting_review"]["kind"].astext.in_(VERIFIED_KINDS),
                        and_(
                            so["accounting_review"]["record_type"].astext == "invoice",
                            or_(
                                so["accounting_review"]["kind"].astext.is_(None),
                                so["accounting_review"]["kind"].astext == "invoice_tax",
                            ),
                        ),
                    ),
                    so["accounting_execution"]["version"].astext == "1",
                    so["status"].astext.in_(("executing", "indeterminate", "approved")),
                    cast(so["accounting_execution"]["attempts"].astext, Integer) < MAX_ATTEMPTS,
                    so["accounting_execution"]["next_at"].astext <= now.isoformat(),
                    or_(
                        so["accounting_recheck"]["status"].astext.is_(None),
                        so["accounting_recheck"]["status"].astext != "queued",
                    ),
                )
                .order_by(so["accounting_execution"]["next_at"].astext, ChatMessage.id)
                .limit(limit)
            )
        ).all()
    )


async def _message(db, tenant_id, message_id):
    return await db.scalar(
        select(ChatMessage)
        .where(ChatMessage.tenant_id == tenant_id, ChatMessage.id == message_id)
        .execution_options(populate_existing=True)
    )


async def _authorize_read(db, tenant_id, message, claim):
    from app.mcp.tools.transaction_ops_tools import _authorize
    from app.services.chat.write_confirmation_service import validate_and_extract_confirmation

    so = message.structured_output
    actor = UUID(claim["approved_by"])
    session_owner = await db.scalar(
        select(ChatSession.user_id).where(ChatSession.id == message.session_id, ChatSession.tenant_id == tenant_id)
    )
    audit = await db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.resource_id == str(message.id),
            AuditEvent.action == CLAIM_ACTION,
            AuditEvent.actor_id == actor,
            AuditEvent.payload["evidence_digest"].astext == evidence_digest(so),
            AuditEvent.payload["accepted_at"].astext == claim["accepted_at"],
        )
    )
    if (
        session_owner != actor
        or not audit
        or claim.get("confirmation_id") != str(message.id)
        or claim.get("evidence_digest") != evidence_digest(so)
        or so["accounting_review"].get("tenant_id") != str(tenant_id)
        or not validate_and_extract_confirmation(so, str(message.session_id))[0]
    ):
        raise ValueError("recovery_approval_identity_unverified")
    await _authorize({"db": db, "tenant_id": tenant_id, "actor_id": actor}, create=False, fresh=True)
    return actor


async def refresh_group(db, tenant_id, session_id, parent_id, *, depth=0):
    """Refresh embedded cards under a short parent row lock; never restart children."""
    parent = await db.scalar(
        select(ChatMessage)
        .where(
            ChatMessage.id == UUID(str(parent_id)),
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.session_id == session_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not parent or not (parent.structured_output or {}).get("accounting_group"):
        return
    so = parent.structured_output
    members = []
    for member in so["accounting_group"]["members"]:
        child = (
            await _message(db, tenant_id, UUID(member["confirmation_id"])) if member.get("confirmation_id") else None
        )
        if child and child.session_id == session_id:
            member = {**member, "card": child.structured_output}
            dispatch_result = ((so.get("accounting_group_dispatch") or {}).get("members") or {}).get(
                member["confirmation_id"], {}
            )
            if dispatch_result.get("reason"):
                member["reason"] = dispatch_result["reason"]
            if (child.structured_output.get("accounting_verification") or {}).get("status") == "verified":
                member.pop("reason", None)
        members.append(member)
    eligible_members = [m for m in members if m.get("confirmation_id")]
    verified = sum(
        m["card"].get("status") == "approved"
        and (m["card"].get("accounting_verification") or {}).get("status") == "verified"
        for m in eligible_members
    )
    parent.structured_output = {
        **so,
        "status": "executing"
        if (so.get("accounting_group_dispatch") or {}).get("status") in {"queued", "running"}
        else "approved"
        if eligible_members and verified == len(eligible_members)
        else "indeterminate",
        "accounting_group": {**so["accounting_group"], "members": members},
    }
    parent.content = (
        f"Verified {verified} of {len(eligible_members)} approved corrections. "
        "Full case reconciliation and cash settlement remain separate. Unverified writes are never retried."
    )
    from app.services.transaction_ops.accounting_plan_group import refresh

    await refresh(db, tenant_id, parent)
    if depth < 8 and so.get("accounting_plan_predecessor"):
        await refresh_group(db, tenant_id, session_id, so["accounting_plan_predecessor"], depth=depth + 1)


RECOVERY_READ_CALLS = 8


async def recover(db, tenant_id, message_id, *, now=None, lock_engine=None):
    from app.services.transaction_ops import accounting_recheck, sales_credit
    from app.services.transaction_ops import state_service as state
    from app.services.transaction_ops.accounting_group import accounting_write_slot

    now = now or datetime.now(timezone.utc)
    await set_tenant_context(db, str(tenant_id))
    operation = await state.operation_for_approval(db, tenant_id, "chat_confirmation", message_id)
    if operation is not None:
        return await recover_card(db, tenant_id, operation, now=now, lock_engine=lock_engine)
    message = await _message(db, tenant_id, message_id)
    if not message or not eligible(message.structured_output or {}, now):
        return {"termination_reason": "done", "financial_writes": 0}
    locked = False
    try:
        lock_options = {"lock_engine": lock_engine} if lock_engine is not None else {}
        async with accounting_write_slot(message.structured_output["accounting_review"], **lock_options):
            locked = True
            # The live writer can finish between discovery and lock acquisition.
            message = await _message(db, tenant_id, message_id)
            so = message.structured_output
            if not eligible(so, now):
                return {"termination_reason": "done", "financial_writes": 0}
            claim = {
                **so["accounting_execution"],
                "attempts": so["accounting_execution"]["attempts"] + 1,
                "next_at": (now + DELAY).isoformat(),
                "termination_reason": "interrupted",
            }
            so = {**so, "accounting_execution": claim}
            message.structured_output = so
            # Commit the budget BEFORE reads. A killed worker consumes its attempt.
            await log_event(
                db,
                tenant_id,
                "transaction_ops",
                "accounting_recovery.started",
                actor_type="system",
                resource_type="chat_message",
                resource_id=str(message_id),
                payload={**claim, "financial_writes": 0},
            )
            await db.commit()
            await set_tenant_context(db, str(tenant_id))
            try:
                async with asyncio.timeout(90):
                    actor = await _authorize_read(db, tenant_id, message, claim)
                    if so["accounting_review"].get("kind") == "invoice_sales_adjustment":
                        from app.services.transaction_ops.invoice_discount import verify_after as verify_discount

                        verification = await verify_discount(
                            db, tenant_id, so["accounting_review"], claim.get("receipt")
                        )
                    elif so["accounting_review"].get("kind") == "sales_adjustment_credit":
                        verification = await sales_credit.verify_after(
                            db, tenant_id, so["accounting_review"], claim.get("receipt")
                        )
                    else:
                        from app.services.transaction_ops.tax_correction import verify_after as verify_tax

                        verification = await verify_tax(db, tenant_id, so["accounting_review"], claim.get("receipt"))
                    # Native readers retain exact Decimals. Persist the same
                    # string representation as the immediate approval path,
                    # including unsuccessful readbacks and nested GL rows.
                    verification = json.loads(json.dumps(verification, default=str, allow_nan=False))
            except Exception as exc:
                verification = {"status": "needs_review", "reason": type(exc).__name__, "retry_allowed": False}
            verified = verification.get("status") == "verified"
            claim = {
                **claim,
                "termination_reason": "done"
                if verified
                else "budget"
                if claim["attempts"] >= MAX_ATTEMPTS
                else "error",
            }
            so = {
                **so,
                "status": "approved" if verified else "indeterminate",
                "accounting_verification": verification,
                "accounting_execution": claim,
            }
            message.structured_output = so
            if verified:
                so.pop("error", None)
                try:
                    run = await accounting_recheck.queue(db, tenant_id, message, actor, now=now)
                    so = {**so, "accounting_recheck": {"status": "queued", "run_id": str(run.id)}}
                except Exception as exc:
                    so = {**so, "accounting_recheck": {"status": "not_queued", "reason": type(exc).__name__}}
                    claim["termination_reason"] = "budget" if claim["attempts"] >= MAX_ATTEMPTS else "error"
            message.structured_output = so
            message.content = (
                "The approved accounting correction and GL were verified "
                "by read-only recovery. "
                "No additional financial write was sent. Full case reconciliation and cash settlement remain separate."
                if verified
                else "The interrupted accounting correction is not verified. "
                "The case still needs review. Do not repeat this write."
            )
            await log_event(
                db,
                tenant_id,
                "transaction_ops",
                "accounting_recovery.completed",
                actor_type="system",
                resource_type="chat_message",
                resource_id=str(message_id),
                payload={
                    **claim,
                    "verification": verification,
                    "financial_writes": 0,
                    "accounting_recheck": so.get("accounting_recheck"),
                },
            )
            parent_id = claim["approval_context"].get("group_approval_id")
            if parent_id:
                await refresh_group(db, tenant_id, message.session_id, parent_id)
            await db.commit()
            return {"termination_reason": claim["termination_reason"], "financial_writes": 0}
    except ValueError:
        if locked:
            raise
        # Lock contention is not an outcome and consumes no verification budget.
        await db.rollback()
        return {"termination_reason": "busy", "financial_writes": 0}


async def recover_card(db, tenant_id, operation, *, now, lock_engine=None):
    """One read-only recovery pass for a card the write kernel claimed.

    The ledger row is the truth: an executing attempt past its deadline is settled first
    (unknown if the permit was consumed, refused before effect if not); a settled row gets
    one budgeted readback under its own recovery run, and only a proof moves it to
    verified. The card is then rendered from the row. Nothing here sends.
    """
    from app.services.transaction_ops import accounting_recheck
    from app.services.transaction_ops import state_service as state
    from app.services.transaction_ops.accounting_adapter import ledger_safe
    from app.services.transaction_ops.accounting_group import accounting_write_slot
    from app.services.transaction_ops.tax_correction import verify_after

    await state.recover_expired_operation(db, tenant_id, operation.id, now=now)
    operation = await state._one(db, tenant_id, TransactionOperation, operation.id)
    if operation.status not in state.SETTLED:
        # Terminal already, or still executing before its deadline: the sender may be alive.
        reason = "done" if operation.status in state.TERMINAL else "busy"
        return {"termination_reason": reason, "financial_writes": 0}
    message = await _message(db, tenant_id, operation.approval_id)
    if message is None:
        return {"termination_reason": "error", "financial_writes": 0}
    so = message.structured_output or {}
    p = so.get("accounting_review") or {}
    approver = (operation.result_json or {}).get("approved_by")
    locked = False
    try:
        lock_options = {"lock_engine": lock_engine} if lock_engine is not None else {}
        async with accounting_write_slot(p, **lock_options):
            locked = True
            try:
                run = await state.create_operation_recovery(
                    db,
                    tenant_id,
                    operation.id,
                    now=now,
                    config_id=_uuid_or_none(p.get("config_id")),
                    order_reference=p.get("order_reference"),
                )
            except state.StateError as exc:
                if exc.code == "operation_not_recoverable":
                    return {"termination_reason": "done", "financial_writes": 0}
                raise
            if run.status == "finished":
                return {"termination_reason": "done", "financial_writes": 0}
            run_id, operation_id = run.id, operation.id  # a rollback below expires the ORM rows
            receipt = (operation.result_json or {}).get("receipt")
            token = await state.claim_run(db, tenant_id, run_id, now=now)
            if token is None:
                return {"termination_reason": "busy", "financial_writes": 0}
            reason, proof, verification = "stall", None, None
            try:
                permit = await state.reserve_budget(
                    db, tenant_id, run_id, lease_token=token, api_calls=RECOVERY_READ_CALLS, orders=1, now=now
                )
                if not permit:
                    raise TimeoutError
                async with asyncio.timeout(90):
                    verification = await verify_after(db, tenant_id, p, receipt=receipt)
                verification = json.loads(json.dumps(verification, default=str, allow_nan=False))
                if verification.get("status") == "verified":
                    proof, reason = ledger_safe(verification), "done"
            except Exception as exc:
                # Provider helpers can fail inside a database transaction; release it
                # before recording. The committed lease and spend are never refunded.
                await db.rollback()
                await set_tenant_context(db, str(tenant_id))
                reason = "budget" if isinstance(exc, TimeoutError) else "error"
                verification = {"status": "needs_review", "reason": type(exc).__name__, "retry_allowed": False}
            operation = await state.finish_operation_recovery(
                db, tenant_id, run_id, lease_token=token, reason=reason, proof=proof, now=now
            )
            verified = operation.status == "verified"
            message = await _message(db, tenant_id, operation.approval_id)
            so = {
                **(message.structured_output or {}),
                "status": "approved" if verified else "indeterminate",
                "accounting_verification": {
                    **verification,
                    "receipt_outcome": "indeterminate",
                    "recovered_by_read": verified,
                },
            }
            if verified:
                so.pop("error", None)
            message.structured_output = so
            recheck = None
            if verified:
                try:
                    recheck_run = await accounting_recheck.queue(db, tenant_id, message, UUID(str(approver)), now=now)
                    recheck = {"status": "queued", "run_id": str(recheck_run.id)}
                except Exception as exc:
                    recheck = {"status": "not_queued", "reason": type(exc).__name__}
                    reason = "error"
                message.structured_output = {**so, "accounting_recheck": recheck}
            message.content = (
                "The approved accounting correction and GL were verified by read-only recovery. "
                "No additional financial write was sent. Full case reconciliation and cash settlement remain separate."
                if verified
                else "The interrupted accounting correction is not verified. "
                "The case still needs review. Do not repeat this write."
            )
            await log_event(
                db,
                tenant_id,
                "transaction_ops",
                "accounting_recovery.completed",
                actor_type="system",
                resource_type="chat_message",
                resource_id=str(message.id),
                payload={
                    "operation_id": str(operation_id),
                    "approved_by": approver,
                    "termination_reason": reason,
                    "verification": verification,
                    "financial_writes": 0,
                    "accounting_recheck": recheck,
                },
            )
            await db.commit()
            return {"termination_reason": reason, "financial_writes": 0}
    except ValueError:
        if locked:
            raise
        # Lock contention is not an outcome and consumes no verification budget.
        await db.rollback()
        return {"termination_reason": "busy", "financial_writes": 0}


def _uuid_or_none(value):
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None
