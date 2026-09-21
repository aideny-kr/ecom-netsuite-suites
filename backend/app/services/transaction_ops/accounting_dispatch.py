"""Durable dispatch of exact, human-decided groups through the existing write path.

The database is the outbox; broker delivery conveys no authority. Every child is
reserved before invocation. Interrupted reservations are inspected, never resent.
An unconfirmed outcome durably stops untouched members. No model is spawned.
"""

import asyncio
import hashlib
from copy import deepcopy
from datetime import timedelta, timezone
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.write_confirmation_service import validate_and_extract_confirmation
from app.services.transaction_ops.accounting_group import GROUP_TOOL, bounded_map, digest

ACCEPTED = "accounting_group.dispatch.accepted"
SLICE_SIZE = 30


def stop_queued(work, trigger, now):
    """Called under the parent row lock, atomically with the uncertain outcome.

    The stop survives recovery of the triggering child. Untouched approvals need
    fresh human review; already reserved children are inspected, never replayed.
    """
    work.setdefault("stopped_after", trigger)
    for member in work["members"].values():
        if member["status"] == "queued":
            member.update(
                status="blocked",
                reason="Not submitted after an unconfirmed group outcome. Prepare a fresh approval after review.",
                finished_at=now.isoformat(),
            )


async def clock(db):
    return (await db.scalar(select(text("clock_timestamp()")))).astimezone(timezone.utc)


async def message(db, tenant_id, identifier, *, lock=False):
    await set_tenant_context(db, str(tenant_id))
    query = select(ChatMessage).where(ChatMessage.tenant_id == tenant_id, ChatMessage.id == identifier)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def accept_dispatch(db, tenant_id, session, parent, so, action, actor_id, correlation_id):
    """Called only after group and durable-child validation; caller commits CAS."""
    accepted = (await clock(db)).isoformat()
    authorization = {
        "version": 1,
        "tenant_id": str(tenant_id),
        "session_id": str(session.id),
        "parent_id": str(parent.id),
        "actor_id": str(actor_id),
        "action": action,
        "accepted_at": accepted,
        "manifest_digest": so["tool_input"]["manifest_digest"],
        "members": [
            {"confirmation_id": m["confirmation_id"], "case_id": m["case_id"], "card_digest": digest(m["card"])}
            for m in so["accounting_group"]["members"]
            if m.get("confirmation_id")
        ],
    }
    audit = await log_event(
        db,
        tenant_id,
        "transaction_ops",
        ACCEPTED,
        actor_id=actor_id,
        resource_type="chat_message",
        resource_id=str(parent.id),
        correlation_id=correlation_id,
        payload={"authorization_digest": digest(authorization), "financial_writes": 0},
    )
    work = {
        "version": 1,
        "status": "queued",
        "next_at": accepted,
        "accepted_at": accepted,
        "authorization": authorization,
        "audit_id": str(audit.id),
        "correlation_id": correlation_id,
        "members": {m["confirmation_id"]: {"status": "queued"} for m in authorization["members"]},
    }
    return {**so, "accounting_group_dispatch": work}


async def publish(tenant_id, parent_id):
    from app.services.transaction_ops.action_scheduler import _dispatch

    stats = {"dispatched": 0, "dispatch_failed": 0}
    await _dispatch(tenant_id, "group", parent_id, stats)
    return stats


async def candidates(db, tenant_id, now=None, *, limit=201):
    await set_tenant_context(db, str(tenant_id))
    now = await clock(db)  # Host clock skew cannot strand or prematurely resume work.
    work = ChatMessage.structured_output["accounting_group_dispatch"]
    return list(
        await db.scalars(
            select(ChatMessage.id)
            .where(
                ChatMessage.tenant_id == tenant_id,
                work["version"].astext == "1",
                work["status"].astext.in_(("queued", "running")),
                work["next_at"].astext <= now.isoformat(),
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
            .limit(limit)
        )
    )


async def validate_authority(db, tenant_id, parent):
    """Mutable UI receipts cannot widen the immutable accepted authorization."""
    so = parent.structured_output
    work = so["accounting_group_dispatch"]
    auth = work["authorization"]
    valid, name, params = validate_and_extract_confirmation(so, str(parent.session_id))
    ids = [m["confirmation_id"] for m in auth["members"]]
    if (
        work.get("version") != 1
        or auth.get("version") != 1
        or auth.get("tenant_id") != str(tenant_id)
        or auth.get("parent_id") != str(parent.id)
        or auth.get("session_id") != str(parent.session_id)
        or auth.get("action") not in {"approve", "reject"}
        or not 1 <= len(ids) <= 500
        or len(set(ids)) != len(ids)
        or set(work["members"]) != set(ids)
        or not valid
        or name != GROUP_TOOL
        or params != {"manifest_digest": auth["manifest_digest"], "confirmation_ids": ids}
    ):
        raise ValueError("group_dispatch_authority_invalid")
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == parent.session_id,
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == UUID(auth["actor_id"]),
        )
    )
    audit = await db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.id == UUID(work["audit_id"]),
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == ACCEPTED,
            AuditEvent.resource_id == str(parent.id),
            AuditEvent.actor_id == UUID(auth["actor_id"]),
            AuditEvent.payload["authorization_digest"].astext == digest(auth),
        )
    )
    if session is None or audit is None:
        raise ValueError("group_dispatch_approval_unverified")
    return auth


async def invoke_child(db, tenant_id, parent_id, auth, member):
    """No financial implementation here: use the normal signed confirmation path."""
    from app.mcp.tools.transaction_ops_tools import _authorize
    from app.services.chat.orchestrator import run_chat_turn

    child = await message(db, tenant_id, UUID(member["confirmation_id"]))
    if child is None or child.session_id != UUID(auth["session_id"]):
        raise ValueError("group_child_unavailable")
    if digest(child.structured_output) != member["card_digest"]:
        raise ValueError("group_child_changed")
    await _authorize({"db": db, "tenant_id": tenant_id, "actor_id": UUID(auth["actor_id"])}, create=True, fresh=True)
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == child.session_id,
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == UUID(auth["actor_id"]),
        )
    )
    if session is None:
        raise ValueError("group_session_unavailable")
    db.info["accounting_authorization_session_factory"] = async_sessionmaker(db.bind, expire_on_commit=False)
    db.info["accounting_worker"] = True
    db.info["accounting_group_execution"] = {
        "group_approval_id": str(parent_id),
        "manifest_digest": auth["manifest_digest"],
        "confirmation_id": member["confirmation_id"],
        "card_digest": member["card_digest"],
        "session_id": auth["session_id"],
        "tenant_id": str(tenant_id),
        "action": auth["action"],
    }
    try:
        error = None
        async with asyncio.timeout(150):
            async for _event in run_chat_turn(
                db=db,
                session=session,
                user_message="",
                user_id=UUID(auth["actor_id"]),
                tenant_id=tenant_id,
                write_confirm={"action": auth["action"], "confirmation_id": member["confirmation_id"]},
            ):
                if _event.get("type") == "error" and error is None:
                    error = {
                        "code": _event.get("code"),
                        "reason": str(_event.get("error", "Correction needs review."))[:1000],
                    }
        return error
    finally:
        db.info.pop("accounting_group_execution", None)
        db.info.pop("accounting_authorization_session_factory", None)
        db.info.pop("accounting_worker", None)


def outcome(card, action, *, interrupted=False):
    status = card.get("status")
    if action == "reject" and status == "rejected":
        return {"status": "rejected"}
    if status == "approved" and (card.get("accounting_verification") or {}).get("status") == "verified":
        return {"status": "verified"}
    if status == "failed" and card.get("repair_exit_reason") == "dispatch_disabled":
        # The orchestrator's switch halted this member after its claim. Nothing was sent
        # (audited as a zero-write precondition failure), so there is nothing to verify;
        # the member needs a fresh approval once dispatch is re-enabled.
        return {
            "status": "blocked",
            "reason": (
                "Sending was disabled by the operator before this correction was sent. Nothing was "
                "sent; prepare a fresh approval once dispatch is re-enabled."
            ),
        }
    if card.get("accounting_execution") or status in {"executing", "indeterminate", "approved"}:
        return {
            "status": "verification_pending",
            "reason": "Recorded attempt requires verification; it will not be resent.",
        }
    return {
        "status": "needs_review",
        "reason": (
            "Dispatch was interrupted. Review the recorded outcome and prepare a fresh approval if needed."
            if interrupted
            else "No verified correction was completed. Review this order's evidence and approval result."
        ),
    }


async def run_slice(db, tenant_id, parent_id, *, session_factory=None):
    """One durable group leader, up to three independent child sessions, 30 per slice.

    The advisory lock dies with the process/connection. A new leader can inspect
    interrupted child reservations before deciding whether untouched members can run.
    """
    factory = session_factory or async_sessionmaker(db.bind, expire_on_commit=False)
    key = int.from_bytes(
        hashlib.sha256(f"accounting-dispatch:{tenant_id}:{parent_id}".encode()).digest()[:8], "big", signed=True
    )
    async with db.bind.connect() as leader:
        if not await leader.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
            return {"status": "busy"}
        try:
            return await _drain(db, tenant_id, parent_id, factory)
        finally:
            try:
                await asyncio.shield(leader.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key}))
            except BaseException:
                await leader.invalidate()


async def _drain(db, tenant_id, parent_id, factory):
    parent = await message(db, tenant_id, parent_id)
    if parent is None:
        return {"status": "unavailable"}
    work = (parent.structured_output or {}).get("accounting_group_dispatch") or {}
    if work.get("version") != 1 or work.get("status") not in {"queued", "running"}:
        return {"status": "not_pending"}
    try:
        auth = await validate_authority(db, tenant_id, parent)
    except (ValueError, KeyError, TypeError):
        parent = await message(db, tenant_id, parent_id, lock=True)
        parent.structured_output = {
            **parent.structured_output,
            "status": "indeterminate",
            "accounting_group_dispatch": {
                **parent.structured_output["accounting_group_dispatch"],
                "status": "needs_review",
            },
        }
        parent.content = (
            "Group dispatch could not verify its recorded approval. No further corrections were dispatched."
        )
        await log_event(
            db,
            tenant_id,
            "transaction_ops",
            "accounting_group.dispatch.authority_rejected",
            resource_type="chat_message",
            resource_id=str(parent_id),
            payload={"financial_writes": 0},
        )
        await db.commit()
        return {"status": "needs_review"}
    # Row-locked updates merge current UI/verification changes, never overwrite them.
    parent = await message(db, tenant_id, parent_id, lock=True)
    work = deepcopy(parent.structured_output["accounting_group_dispatch"])
    if auth["action"] == "approve":
        trigger = work.get("stopped_after") or next(
            (
                key
                for key, member in work["members"].items()
                if member["status"] in {"verification_pending", "needs_review"}
            ),
            None,
        )
        if trigger:
            stop_queued(work, trigger, await clock(db))
    work.update(status="running", next_at=((await clock(db)) + timedelta(minutes=1)).isoformat())
    parent.structured_output = {**parent.structured_output, "accounting_group_dispatch": work}
    await db.commit()
    remaining = [
        m for m in auth["members"] if work["members"][m["confirmation_id"]]["status"] in {"queued", "dispatching"}
    ]
    remaining.sort(key=lambda m: work["members"][m["confirmation_id"]].get("attempts", 0))

    halted = []

    async def process(member):
        identifier = member["confirmation_id"]
        async with factory() as child_db:
            parent = await message(child_db, tenant_id, parent_id, lock=True)
            current = deepcopy(parent.structured_output["accounting_group_dispatch"])
            before = current["members"][identifier]["status"]
            error = None
            if before not in {"queued", "dispatching"}:
                await child_db.rollback()
                return
            if before == "queued" and auth["action"] == "approve" and not settings.TRANSACTION_OPS_DISPATCH_ENABLED:
                # Operator kill switch: never reserve or invoke this member. It stays
                # queued and untouched, and resumes when dispatch is re-enabled. A
                # member already dispatching is only inspected below, never resent.
                # A rejection sends nothing, so it drains regardless of the switch.
                halted.append(identifier)
                await child_db.rollback()
                return
            if before == "queued":
                current["members"][identifier] = {
                    "status": "dispatching",
                    "started_at": (await clock(child_db)).isoformat(),
                    "attempts": current["members"][identifier].get("attempts", 0) + 1,
                }
                parent.structured_output = {**parent.structured_output, "accounting_group_dispatch": current}
                await log_event(
                    child_db,
                    tenant_id,
                    "transaction_ops",
                    "accounting_group.case.dispatch_reserved",
                    actor_id=UUID(auth["actor_id"]),
                    resource_type="chat_message",
                    resource_id=identifier,
                    payload={
                        "group_approval_id": str(parent_id),
                        "manifest_digest": auth["manifest_digest"],
                        "financial_writes": 0,
                    },
                )
                await child_db.commit()  # Must precede any possible external write.
                try:
                    error = await invoke_child(child_db, tenant_id, parent_id, auth, member)
                except Exception as exc:
                    # Preserve known actionable diagnoses without exposing raw
                    # provider exceptions, URLs or credential-bearing messages.
                    reasons = {
                        "group_child_unavailable": "The approved correction is no longer available in this chat.",
                        "group_child_changed": "The correction changed after group approval. Review a fresh proposal.",
                        "group_session_unavailable": "The original approver's chat session is no longer available.",
                    }
                    error = {"reason": reasons.get(str(exc), f"Correction needs review ({type(exc).__name__}).")}
            await child_db.rollback()
            child = await message(child_db, tenant_id, UUID(identifier))
            value = child.structured_output if child and child.session_id == UUID(auth["session_id"]) else {}
            result = outcome(value, auth["action"], interrupted=before == "dispatching")
            if result["status"] == "needs_review" and error:
                result["reason"] = error["reason"]
            # This code is emitted only by the account-slot guard BEFORE the
            # child CAS/external write. Identical pending evidence is required;
            # any recorded attempt or uncertain result permanently forbids retry.
            if (
                before == "queued"
                and (error or {}).get("code") == "accounting_capacity_busy"
                and value.get("status") == "pending"
                and not value.get("accounting_execution")
                and digest(value) == member["card_digest"]
                and current["members"][identifier]["attempts"] < 6
            ):
                result = {"status": "queued", "reason": "Waiting for this account's available processing capacity."}
            parent = await message(child_db, tenant_id, parent_id, lock=True)
            current = deepcopy(parent.structured_output["accounting_group_dispatch"])
            current["members"][identifier] = {
                **current["members"][identifier],
                **result,
                "finished_at": (await clock(child_db)).isoformat(),
            }
            if auth["action"] == "approve" and (
                current.get("stopped_after") or result["status"] in {"verification_pending", "needs_review"}
            ):
                first_stop = not current.get("stopped_after")
                stop_queued(current, identifier, await clock(child_db))
                if first_stop:
                    await log_event(
                        child_db,
                        tenant_id,
                        "transaction_ops",
                        "accounting_group.dispatch.stopped",
                        actor_id=UUID(auth["actor_id"]),
                        resource_type="chat_message",
                        resource_id=str(parent_id),
                        payload={"confirmation_id": identifier, "reason": "unconfirmed_outcome", "financial_writes": 0},
                    )
            parent.structured_output = {**parent.structured_output, "accounting_group_dispatch": current}
            await log_event(
                child_db,
                tenant_id,
                "transaction_ops",
                "accounting_group.case.deferred" if result["status"] == "queued" else "accounting_group.case.completed",
                actor_id=UUID(auth["actor_id"]),
                resource_type="chat_message",
                resource_id=identifier,
                payload={
                    "group_approval_id": str(parent_id),
                    "approved_by": auth["actor_id"] if auth["action"] == "approve" else None,
                    "case_id": member["case_id"],
                    **result,
                    "verification": value.get("accounting_verification"),
                },
            )
            await child_db.commit()

    # On restart, a lost dispatch receipt must be classified before any new
    # reservation. Sorting by attempts did the opposite and admitted new writes.
    interrupted = [m for m in remaining if work["members"][m["confirmation_id"]]["status"] == "dispatching"]
    for member in interrupted:
        await process(member)
    await bounded_map([m for m in remaining if m not in interrupted][:SLICE_SIZE], process)
    parent = await message(db, tenant_id, parent_id, lock=True)
    work = deepcopy(parent.structured_output["accounting_group_dispatch"])
    pending = sum(m["status"] in {"queued", "dispatching"} for m in work["members"].values())
    waiting = pending and all(m.get("attempts", 0) > 0 for m in work["members"].values() if m["status"] == "queued")
    # A halted group re-checks the switch every five minutes instead of spinning on
    # the collector's cadence; the marker keeps the audit to one row per halt.
    delay = timedelta(minutes=5) if halted else timedelta(seconds=30 if waiting else 0)
    newly_halted = bool(halted) and not work.get("dispatch_disabled")
    work.update(status="queued" if pending else "finished", next_at=((await clock(db)) + delay).isoformat())
    if halted:
        work["dispatch_disabled"] = True
    else:
        work.pop("dispatch_disabled", None)
    if not pending:
        work["finished_at"] = work["next_at"]
    parent.structured_output = {**parent.structured_output, "accounting_group_dispatch": work}
    from app.services.transaction_ops.accounting_recovery import refresh_group

    await db.flush()
    await refresh_group(db, tenant_id, parent.session_id, parent_id)
    if auth["action"] == "reject":
        rejected = sum(m["status"] == "rejected" for m in work["members"].values())
        parent.structured_output = {
            **parent.structured_output,
            "status": "executing" if pending else "rejected" if rejected == len(work["members"]) else "indeterminate",
        }
        parent.content = f"Rejected {rejected} of {len(work['members'])} proposed corrections."
    await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting_group.dispatch.progress",
        actor_id=UUID(auth["actor_id"]),
        resource_type="chat_message",
        resource_id=str(parent_id),
        payload={"status": work["status"], "remaining": pending, "orders": len(work["members"]), "model_calls": 0},
    )
    if newly_halted:
        await log_event(
            db,
            tenant_id,
            "transaction_ops",
            "accounting_group.dispatch.disabled",
            actor_id=UUID(auth["actor_id"]),
            resource_type="chat_message",
            resource_id=str(parent_id),
            payload={
                "group_approval_id": str(parent_id),
                "halted_members": len(halted),
                "setting": "TRANSACTION_OPS_DISPATCH_ENABLED",
                "financial_writes": 0,
            },
        )
    if not pending:
        await log_event(
            db,
            tenant_id,
            "transaction_ops",
            "accounting_group.completed",
            actor_id=UUID(auth["actor_id"]),
            resource_type="chat_message",
            resource_id=str(parent_id),
            payload={
                "status": parent.structured_output["status"],
                "eligible": len(work["members"]),
                "verified": sum(m["status"] == "verified" for m in work["members"].values()),
                "rejected": sum(m["status"] == "rejected" for m in work["members"].values()),
                "confirmation_ids": list(work["members"]),
            },
        )
    await db.commit()
    if halted:
        status = "blocked"
    elif waiting:
        status = "waiting"
    else:
        status = work["status"]
    return {"status": status, "remaining": pending, "orders": len(work["members"])}
