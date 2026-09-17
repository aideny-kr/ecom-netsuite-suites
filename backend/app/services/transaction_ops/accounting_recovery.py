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
from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionOperation, TransactionRun
from app.services.audit_service import log_event
from app.services.transaction_ops.treatments import VERIFIED_KINDS

MAX_ATTEMPTS = 3
DELAY = timedelta(minutes=5)
# A card CAS-accepted to executing whose process died before the ledger claim: after this
# long with no ledger row and no legacy claim, nothing was sent (no row, no permit).
ORPHAN_GRACE = timedelta(minutes=10)
CLAIM_ACTION = "accounting_correction.approval_claimed"


def evidence_digest(so):
    # Fingerprint exact persisted JSON, not a monetary business key. Native
    # source evidence includes JSON numbers; never round or calculate with them.
    value = {k: so.get(k) for k in ("tool_name", "tool_input", "accounting_review")}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def execution_claim(so, confirmation_id, actor_id, context, *, now):
    """The card's OWN claim, the shape every accounting card carried before the ledger.

    No production path writes it any more: every accounting card claims on the ledger
    (chat_confirmation.claim) and carries a projection of its row in this shape. It stays
    for the legacy recovery scan's tests until that scan is deleted."""
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
    so = ChatMessage.structured_output
    card_never_heard = exists(
        select(ChatMessage.id).where(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.id == TransactionOperation.approval_id,
            so["status"].astext == "executing",
        )
    )
    return list(
        (
            await db.scalars(
                select(TransactionOperation.approval_id)
                .where(
                    TransactionOperation.tenant_id == tenant_id,
                    TransactionOperation.approval_kind == "chat_confirmation",
                    or_(
                        and_(
                            ~busy_or_done,
                            or_(
                                and_(
                                    TransactionOperation.status.in_(state.OPEN),
                                    TransactionOperation.deadline_at <= now,
                                ),
                                TransactionOperation.status == "unknown",
                            ),
                        ),
                        # the row settled but the sender died before the card was written
                        and_(TransactionOperation.status.in_(state.TERMINAL), card_never_heard),
                    ),
                )
                .order_by(TransactionOperation.attempted_at, TransactionOperation.id)
                .limit(limit)
            )
        ).all()
    )


async def orphan_candidates(db, tenant_id, now, *, limit):
    """Accounting cards accepted to executing that never reached a claim of either kind:
    no ledger row for the card, no legacy accounting_execution, and older than the grace
    period. No row means no permit, so nothing was sent; they are released, not read."""
    await set_tenant_context(db, str(tenant_id))
    so = ChatMessage.structured_output
    claimed = exists(
        select(TransactionOperation.id).where(
            TransactionOperation.tenant_id == tenant_id,
            TransactionOperation.approval_kind == "chat_confirmation",
            TransactionOperation.approval_id == ChatMessage.id,
        )
    )
    return list(
        (
            await db.scalars(
                select(ChatMessage.id)
                .where(
                    ChatMessage.tenant_id == tenant_id,
                    so["accounting_review"].astext.isnot(None),
                    so["status"].astext == "executing",
                    so["operation_id"].astext.is_(None),
                    so["accounting_execution"].astext.is_(None),
                    ChatMessage.updated_at <= now - ORPHAN_GRACE,
                    ~claimed,
                )
                .order_by(ChatMessage.updated_at, ChatMessage.id)
                .limit(limit)
            )
        ).all()
    )


async def candidates(db, tenant_id, now, *, limit):
    """Cards due for recovery: those the kernel claimed (the ledger says so), those that
    never reached a claim (released), and, for one release after the native amendment card
    moved onto the kernel (2026-09-17), those still carrying their own legacy claim."""
    found = await ledger_candidates(db, tenant_id, now, limit=limit)
    for more in (
        await orphan_candidates(db, tenant_id, now, limit=limit),
        await _legacy_candidates(db, tenant_id, now, limit=limit),
    ):
        found += [m for m in more if m not in found]
    return found[:limit]


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
    from app.services.transaction_ops.chat_confirmation import session_owner as _owner

    so = message.structured_output
    actor = UUID(claim["approved_by"])
    session_owner = await _owner(db, tenant_id, message)
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


def _orphaned(message, now) -> bool:
    so = message.structured_output or {}
    return (
        bool(so.get("accounting_review"))
        and so.get("status") == "executing"
        and not so.get("operation_id")
        and not so.get("accounting_execution")
        and message.updated_at <= now - ORPHAN_GRACE
    )


async def _locked_message(db, tenant_id, message_id):
    return await db.scalar(
        select(ChatMessage)
        .where(ChatMessage.tenant_id == tenant_id, ChatMessage.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def tenants_with_open_cards(db, now):
    """Tenants holding a kernel-claimed card that is not terminal, or a card that never
    reached a claim (an orphan past its grace); the scan reaches them whether or not the
    scheduled feature's flags are on."""
    from app.services.transaction_ops import state_service as state

    so = ChatMessage.structured_output
    card_never_heard = exists(
        select(ChatMessage.id).where(
            ChatMessage.id == TransactionOperation.approval_id, so["status"].astext == "executing"
        )
    )
    claimed = list(
        (
            await db.scalars(
                select(TransactionOperation.tenant_id)
                .where(
                    TransactionOperation.approval_kind == "chat_confirmation",
                    or_(
                        TransactionOperation.status.in_(state.IN_FLIGHT),
                        and_(TransactionOperation.status.in_(state.TERMINAL), card_never_heard),
                    ),
                )
                .distinct()
            )
        ).all()
    )
    orphaned = list(
        (
            await db.scalars(
                select(ChatMessage.tenant_id)
                .where(
                    so["accounting_review"].astext.isnot(None),
                    so["status"].astext == "executing",
                    so["operation_id"].astext.is_(None),
                    so["accounting_execution"].astext.is_(None),
                    ChatMessage.updated_at <= now - ORPHAN_GRACE,
                )
                .distinct()
            )
        ).all()
    )
    return claimed + [t for t in orphaned if t not in claimed]


async def _release_orphan(db, tenant_id, message_id, now):
    """Release a card whose process died between its own claim and the ledger's: no ledger
    row means no permit was minted, so nothing was sent and the intent is free again. The
    card is re-read under a row lock: a sender that caught up in the meantime keeps it."""
    from app.services.transaction_ops import state_service as state

    message = await _locked_message(db, tenant_id, message_id)
    if (
        message is None
        or not _orphaned(message, now)
        or await state.operation_for_approval(db, tenant_id, "chat_confirmation", message_id) is not None
    ):
        await db.rollback()
        return {"termination_reason": "busy", "financial_writes": 0}
    reason = "The approval was interrupted before it was claimed on the ledger; nothing was sent."
    message.structured_output = {**message.structured_output, "status": "failed", "error": reason}
    message.content = reason + " Prepare a fresh correction to try again."
    await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting_correction.precondition_failed",
        actor_type="system",
        resource_type="chat_message",
        resource_id=str(message.id),
        payload={"financial_writes": 0, "reason": reason, "released_by": "accounting_recovery"},
        status="error",
    )
    await db.commit()
    return {"termination_reason": "done", "financial_writes": 0}


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
    if message is not None and _orphaned(message, now):
        return await _release_orphan(db, tenant_id, message_id, now)
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
    from app.services.transaction_ops import state_service as state
    from app.services.transaction_ops.accounting_adapter import json_copy, ledger_view
    from app.services.transaction_ops.accounting_group import accounting_write_slot
    from app.services.transaction_ops.tax_correction import verify_after

    await state.recover_expired_operation(db, tenant_id, operation.id, now=now)
    operation = await state._one(db, tenant_id, TransactionOperation, operation.id)
    message = await _message(db, tenant_id, operation.approval_id)
    if message is None:
        # The card is gone (a deleted session): the row cannot be read for, and must not be
        # re-dispatched every minute. A person decides.
        if operation.status in state.SETTLED:
            await state.complete_operation(
                db, tenant_id, operation.id, outcome="needs_review", result_json={"code": "card_missing"}
            )
            return {"termination_reason": "blocked", "financial_writes": 0}
        return {"termination_reason": "done" if operation.status in state.TERMINAL else "busy", "financial_writes": 0}
    if operation.status not in state.SETTLED:
        if operation.status in state.TERMINAL and (message.structured_output or {}).get("status") == "executing":
            # The row settled (by expiry, or by a sender that died after completing it)
            # while the card never heard: render the card from the row.
            await render_settled_card(db, tenant_id, message, operation, now=now)
            return {"termination_reason": "done", "financial_writes": 0}
        # Terminal already, or still executing before its deadline: the sender may be alive.
        reason = "done" if operation.status in state.TERMINAL else "busy"
        return {"termination_reason": reason, "financial_writes": 0}
    so = message.structured_output or {}
    p = so.get("accounting_review") or {}
    if evidence_digest(so) != (operation.result_json or {}).get("evidence_digest"):
        # The card is not the one that was claimed: nothing on it may drive a read.
        return await _escalate(db, tenant_id, message, operation, "confirmation_changed", now=now)
    locked = False
    try:
        lock_options = {"lock_engine": lock_engine} if lock_engine is not None else {}
        async with accounting_write_slot(p, **lock_options):
            locked = True
            try:
                run = await state.create_operation_recovery(db, tenant_id, operation.id, now=now)
            except state.StateError as exc:
                if exc.code == "operation_not_recoverable":
                    return {"termination_reason": "done", "financial_writes": 0}
                # Nothing to read under (no scope, a disabled config, a vanished config): a
                # person decides, the document is freed, the scan stops re-dispatching.
                return await _escalate(db, tenant_id, message, operation, exc.code, now=now)
            if run.status == "finished":
                return {"termination_reason": "done", "financial_writes": 0}
            run_id = run.id  # a rollback below expires the ORM rows
            # A receipt, or the identity a non-receipt answer named: a readback that sees
            # a different record than the approval refuses instead of reconciling it away.
            receipt = state.recorded_answer(operation)
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
                verification = json_copy(verification)
                if verification.get("status") == "verified":
                    # The row keeps what the provider's adapter would keep (the native
                    # readback carries whole records that would exceed the row's bound).
                    proof, reason = ledger_view(operation.provider, verification), "done"
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
            message = await _message(db, tenant_id, operation.approval_id)
            reason = await render_settled_card(
                db, tenant_id, message, operation, now=now, verification=verification, reason=reason
            )
            return {"termination_reason": reason, "financial_writes": 0}
    except ValueError:
        if locked:
            raise
        # Lock contention is not an outcome and consumes no verification budget.
        await db.rollback()
        return {"termination_reason": "busy", "financial_writes": 0}


async def _escalate(db, tenant_id, message, operation, code, *, now):
    from app.services.transaction_ops import state_service as state

    operation = await state.complete_operation(
        db, tenant_id, operation.id, outcome="needs_review", result_json={"code": code}
    )
    await render_settled_card(db, tenant_id, message, operation, now=now)
    return {"termination_reason": "blocked", "financial_writes": 0}


async def render_settled_card(db, tenant_id, message, operation, *, now, verification=None, reason=None):
    """The one place a card is rendered from its ledger row after the sender is gone: the
    projection (keeping the group link), the status, what the readback saw, the audit
    the cross-card history releases a refused intent by, the recheck every verified
    correction queues, the recovery audit, and the group parent. Returns the termination
    reason (``reason`` when given, downgraded to ``error`` if the recheck cannot queue)."""
    from app.services.transaction_ops import accounting_recheck
    from app.services.transaction_ops import state_service as state
    from app.services.transaction_ops.chat_confirmation import execution_projection

    result = operation.result_json or {}
    approver = result.get("approved_by")
    stored = message.structured_output or {}
    context = (stored.get("accounting_execution") or {}).get("approval_context")
    so = {
        **stored,
        "operation_id": str(operation.id),
        "accounting_execution": execution_projection(message.id, operation, context),
    }
    reason = reason or ("done" if operation.status in state.TERMINAL else "stall")
    recheck = None
    receipt_outcome = "accepted" if result.get("receipt") else "indeterminate"
    if operation.status == "rejected_before_effect":
        text = "The approval was interrupted before any send; nothing was sent."
        so.update(status="failed", error=text)
        message.content = text + " Prepare a fresh correction to try again."
        # the same audit every other before-effect refusal writes: the cross-card
        # history check releases the intent by it
        await log_event(
            db,
            tenant_id,
            "transaction_ops",
            "accounting_correction.precondition_failed",
            actor_type="system",
            resource_type="chat_message",
            resource_id=str(message.id),
            payload={"approved_by": approver, "financial_writes": 0, "reason": result.get("code") or text},
            status="error",
        )
    elif operation.status == "verified":
        seen = verification or result.get("verification") or {"status": "verified"}
        so.update(
            status="approved",
            accounting_verification={**seen, "receipt_outcome": receipt_outcome, "recovered_by_read": True},
        )
        so.pop("error", None)
        message.content = (
            "The approved accounting correction and GL were verified by read-only recovery. "
            "No additional financial write was sent. Full case reconciliation and cash settlement remain separate."
        )
        message.structured_output = so  # the recheck queue reads the card as it will be stored
        try:
            scope = result.get("recovery_scope") or {}
            run = await accounting_recheck.queue(
                db, tenant_id, message, UUID(str(approver)), now=now, config_id=scope.get("config_id")
            )
            recheck = {"status": "queued", "run_id": str(run.id)}
        except Exception as exc:
            recheck = {"status": "not_queued", "reason": type(exc).__name__}
            reason = "error"
        so = {**so, "accounting_recheck": recheck}  # a new object: an in-place change after the flush is invisible
    else:
        # committed_unverified (a receipt without proof), unknown, or needs_review
        seen = verification or {"status": "needs_review", "reason": result.get("code"), "retry_allowed": False}
        receipted = operation.status == "committed_unverified"
        so.update(
            status="approved" if receipted else "indeterminate",
            accounting_verification={**seen, "receipt_outcome": receipt_outcome, "recovered_by_read": False},
        )
        if not receipted:
            so["error"] = result.get("code")
        message.content = (
            "The accounting correction was sent and saved but is not verified. The case still needs review. "
            "Do not repeat this write."
            if receipted
            else "The interrupted accounting correction is not verified. The case still needs review. "
            "Do not repeat this write."
        )
    message.structured_output = dict(so)
    await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting_recovery.completed",
        actor_type="system",
        resource_type="chat_message",
        resource_id=str(message.id),
        payload={
            "operation_id": str(operation.id),
            "approved_by": approver,
            "termination_reason": reason,
            "rendered_from_ledger": operation.status,
            "verification": verification,
            "financial_writes": 0,
            "accounting_recheck": recheck,
        },
    )
    parent_id = (context or {}).get("group_approval_id")
    if parent_id:
        await refresh_group(db, tenant_id, message.session_id, parent_id)
    await db.commit()
    return reason
