"""Exact group proposals and bounded execution through the existing signed HITL path.

Group IDs select evidence, never authorize writes. The parent signs a frozen
manifest of individually signed child cards; each child keeps its own CAS,
fresh preconditions, external-call audit and independent invoice/GL proof.
"""

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.core.database import async_session_factory, engine, set_tenant_context
from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.write_confirmation_service import (
    WriteConfirmationPayload,
    mint_confirmation_token,
    validate_and_extract_confirmation,
)

CONCURRENCY = 3
PREPARATION_TIMEOUT = 450  # Leave time to publish an explicit result within the chat budget.
GROUP_TOOL = "transaction_ops_accounting_group_apply"  # Not exposed to model/MCP dispatch.
_authorization_session_factory = async_session_factory


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def bounded_map(items, function, *, stop=None):
    """Only three workers; no task per member, no shared AsyncSession."""
    output = [None] * len(items)
    queue = iter(enumerate(items))

    async def worker():
        for index, item in queue:
            if stop and stop.is_set():
                output[index] = {**item, "reason": "Not submitted after an unconfirmed outcome."}
                continue
            output[index] = await function(item)

    tasks = [asyncio.create_task(worker()) for _ in range(min(CONCURRENCY, len(items)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return output


async def prepare_group_confirmation(*, db, tenant_id, actor_id, correlation_id, session_id, tools, policy, **_):
    from app.mcp.tools.transaction_ops_tools import execute_accounting_evidence
    from app.services.transaction_ops.tax_correction import candidate_confirmation

    selection = db.info.pop("accounting_group_selection", None)
    if not selection:
        raise ValueError("Refresh the scoped issue group before preparing corrections.")

    async def prepare(member):
        async with async_session_factory() as child_db:
            await set_tenant_context(child_db, str(tenant_id))
            context = dict(
                db=child_db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                session_id=session_id,
            )
            try:
                async with asyncio.timeout(120):
                    evidence = await execute_accounting_evidence({"case_id": member["case_id"]}, context=context)
                    prepared = (
                        await candidate_confirmation(
                            db=child_db,
                            tenant_id=tenant_id,
                            actor_id=actor_id,
                            correlation_id=correlation_id,
                            session_id=session_id,
                            task="Prepare an exact correction for human approval",
                            tools=tools,
                            policy=policy,
                            case_id=member["case_id"],
                        )
                        if evidence.get("success")
                        else None
                    )
                    if prepared:
                        card, _note = prepared
                        value = {**card.model_dump(mode="json"), "accounting_group_child": True}
                        # Publish children only in the parent's transaction. A cancelled
                        # preparation must never leave independently actionable orphans.
                        # Keep the per-case evidence/candidate audit, without a ChatMessage.
                        await child_db.commit()
                        return {**member, "confirmation_id": str(uuid.uuid4()), "card": value}
                    reason = "No supported invoice correction; individual investigation required."
            except Exception as exc:
                await child_db.rollback()
                await set_tenant_context(child_db, str(tenant_id))
                reason = f"Preparation needs review ({type(exc).__name__})."
            await log_event(
                child_db,
                tenant_id,
                category="transaction_ops",
                action="accounting_group.case.skipped",
                actor_id=actor_id,
                resource_type="transaction_case",
                resource_id=member["case_id"],
                correlation_id=correlation_id,
                payload={"reason": reason, "financial_writes": 0},
            )
            await child_db.commit()
            return {**member, "reason": reason}

    try:
        async with asyncio.timeout(PREPARATION_TIMEOUT):
            members = await bounded_map(selection["members"], prepare)
    except (asyncio.CancelledError, TimeoutError):
        await asyncio.shield(record_preparation_interrupted(tenant_id, actor_id, session_id, correlation_id, selection))
        raise
    eligible = [m for m in members if m.get("card")]
    targets = [
        (
            m["card"]["accounting_review"]["scope"]["netsuite_account_id"],
            m["card"]["accounting_review"].get("lock_record_type", m["card"]["record_type"]),
            m["card"]["accounting_review"]["record_id"],
        )
        for m in eligible
    ]
    if len(set(targets)) != len(targets):
        raise ValueError("Multiple cases target the same invoice; separate their identities before group approval.")
    group = {
        "group_id": selection["group_id"],
        "scope": selection["scope"],
        "members": members,
        "concurrency": CONCURRENCY,
    }
    params = {"manifest_digest": digest(group), "confirmation_ids": [m["confirmation_id"] for m in eligible]}
    card = WriteConfirmationPayload(
        mutation_type="execute",
        record_type="invoice corrections",
        proposed_fields={"eligible_orders": len(eligible)},
        tool_name=GROUP_TOOL,
        tool_input=params,
        accounting_group=group,
        confirmation_token=mint_confirmation_token(GROUP_TOOL, params, [], session_id),
        invariant_errors=[] if eligible else ["No supported corrections are ready for approval."],
    )
    await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action="accounting_group.proposed",
        actor_id=actor_id,
        resource_type="chat_session",
        resource_id=session_id,
        correlation_id=correlation_id,
        payload={
            "group_id": selection["group_id"],
            **params,
            "case_count": len(members),
            "eligible": len(eligible),
            "financial_writes": 0,
        },
    )
    return card, f"Prepared {len(eligible)} exact invoice corrections for review across {len(members)} orders."


def stage_group_children(db, parent):
    """Stage exact children with their parent, without an intervening await/commit."""
    so = parent.structured_output
    members = validate_manifest(so, str(parent.session_id))
    for member in members:
        if member.get("confirmation_id"):
            db.add(
                ChatMessage(
                    id=uuid.UUID(member["confirmation_id"]),
                    tenant_id=parent.tenant_id,
                    session_id=parent.session_id,
                    role="assistant",
                    content=f"Invoice correction for {member['order_reference']}; review in its group card.",
                    structured_output=member["card"],
                )
            )


async def record_preparation_interrupted(tenant_id, actor_id, session_id, correlation_id, selection):
    """A visible, durable result without publishing any partial financial proposal."""
    async with async_session_factory() as db:
        await set_tenant_context(db, str(tenant_id))
        db.add(
            ChatMessage(
                tenant_id=tenant_id,
                session_id=uuid.UUID(session_id),
                role="assistant",
                content=(
                    "Group preparation was interrupted before the complete review was ready. "
                    "No corrections were submitted or made available for approval. "
                    "Prepare the group again, or narrow the period to reduce its size."
                ),
            )
        )
        await log_event(
            db,
            tenant_id,
            category="transaction_ops",
            action="accounting_group.preparation_interrupted",
            actor_id=actor_id,
            resource_type="chat_session",
            resource_id=session_id,
            correlation_id=correlation_id,
            payload={
                "group_id": selection["group_id"],
                "scope": selection["scope"],
                "case_count": len(selection["members"]),
                "financial_writes": 0,
                "published_corrections": 0,
            },
            status="error",
        )
        await db.commit()


def group_child_context(db, so, confirmation_id, session_id, tenant_id, action):
    """Only the server's claimed-parent runner may dispatch a grouped child."""
    if not so.get("accounting_group_child"):
        return {}
    context = db.info.get("accounting_group_execution") or {}
    if (
        context.get("confirmation_id") != str(confirmation_id)
        or context.get("session_id") != str(session_id)
        or context.get("tenant_id") != str(tenant_id)
        or context.get("action") != action
        or context.get("card_digest") != digest(so)
        or not context.get("group_approval_id")
        or not context.get("manifest_digest")
    ):
        raise ValueError("This correction belongs to a group. Review and approve or reject its exact group card.")
    return {k: context[k] for k in ("group_approval_id", "manifest_digest")}


async def authorize_accounting_write(db, tenant_id, actor_id, tool_name, tool_input):
    from app.mcp.tools.transaction_ops_tools import _authorize
    from app.services.policy_service import evaluate_tool_call, get_active_policy

    # Long-running workers can retain stale policy objects and role relationships.
    # An independent session plus uncached flags reads the current persisted grants.
    async with _authorization_session_factory() as auth_db:
        await set_tenant_context(auth_db, str(tenant_id))
        await _authorize({"db": auth_db, "tenant_id": tenant_id, "actor_id": actor_id}, create=True, fresh=True)
        policy = await get_active_policy(auth_db, tenant_id)
        if not evaluate_tool_call(policy, tool_name, tool_input)["allowed"]:
            raise ValueError("Current policy blocks this correction. No update was sent.")


def validate_manifest(so, session_id):
    valid, name, params = validate_and_extract_confirmation(so, session_id)
    group = so.get("accounting_group") or {}
    members = group.get("members") or []
    eligible = [m for m in members if m.get("confirmation_id")]
    ids = [m["confirmation_id"] for m in eligible]
    if (
        not valid
        or name != GROUP_TOOL
        or not 1 <= len(members) <= 500
        or len(set(ids)) != len(ids)
        or params != {"manifest_digest": digest(group), "confirmation_ids": ids}
    ):
        raise ValueError("The exact group approval is invalid or changed. Prepare a fresh review.")
    targets = []
    for member in eligible:
        card = member["card"]
        p = card.get("accounting_review") or {}
        if (
            not validate_and_extract_confirmation(card, session_id)[0]
            or card.get("status") != "pending"
            or card.get("accounting_group")
            or card.get("editable_slots")
            or card.get("invariant_errors")
            or card.get("unfillable_line_fields")
            or p.get("case_id") != member["case_id"]
        ):
            raise ValueError("A group member is not an exact supported pending correction.")
        targets.append(
            (p["scope"]["netsuite_account_id"], p.get("lock_record_type", card["record_type"]), p["record_id"])
        )
    if len(set(targets)) != len(targets):
        raise ValueError("Overlapping invoice writes cannot be approved together.")
    return members


@asynccontextmanager
async def accounting_write_slot(proposal, *, lock_engine=None):
    """Account-wide cap across processes; serialize all cards for the same invoice.

    Dedicated connection retains session locks across the existing audit/CAS commits.
    Never return a pooled connection with a live advisory lock.
    """
    account = proposal["scope"]["netsuite_account_id"].lower().replace("_", "-")
    keys = []

    def key(value):
        return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)

    async with (lock_engine if lock_engine is not None else engine).connect() as connection:
        try:
            record_type = "invoice" if proposal.get("kind") == "sales_adjustment_credit" else proposal["record_type"]
            record = key(f"accounting-write:{account}:{record_type}:{proposal['record_id']}")
            if not await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": record}):
                raise ValueError("Another approved correction is checking this invoice. No additional update was sent.")
            keys.append(record)
            for index in range(CONCURRENCY):
                slot = key(f"accounting-write:{account}:slot:{index}")
                if await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": slot}):
                    keys.append(slot)
                    break
            else:
                raise ValueError("Three corrections are already running for this account. This update was not sent.")
            yield
        finally:
            try:
                for lock in reversed(keys):
                    await asyncio.shield(connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock}))
            except BaseException:
                await connection.invalidate()
                raise


async def run_group_confirmation(*, db, session, message, so, action, user_id, tenant_id, correlation_id):
    from app.mcp.tools.transaction_ops_tools import _authorize
    from app.services.chat.orchestrator import _cas_claim_write_confirmation, run_chat_turn
    from app.services.policy_service import evaluate_tool_call, get_active_policy

    await _authorize({"db": db, "tenant_id": tenant_id, "actor_id": user_id}, create=True)
    members = validate_manifest(so, str(session.id))
    if action == "approve" and (so.get("invariant_errors") or not so["tool_input"]["confirmation_ids"]):
        raise ValueError("No supported corrections are ready for approval.")
    if session.tenant_id != tenant_id or session.user_id != user_id:
        raise ValueError("Group approval session is unavailable.")
    # Verify durable children before accepting the parent. No model-provided child payloads.
    for member in members:
        if not member.get("confirmation_id"):
            continue
        child = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.id == uuid.UUID(member["confirmation_id"]),
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.session_id == session.id,
            )
        )
        if child is None or digest(child.structured_output) != digest(member["card"]):
            raise ValueError("A reviewed correction changed or was already processed. Refresh group proposals.")
    await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action=f"accounting_group.{action}.requested",
        actor_id=user_id,
        resource_type="chat_message",
        resource_id=str(message.id),
        correlation_id=correlation_id,
        payload={
            "approved_by": str(user_id) if action == "approve" else None,
            "manifest_digest": so["tool_input"]["manifest_digest"],
            "confirmation_ids": so["tool_input"]["confirmation_ids"],
        },
    )
    if not await _cas_claim_write_confirmation(db, message, so, "executing"):
        raise ValueError("This group approval was already claimed.")
    stop = asyncio.Event()

    async def execute(member):
        if not member.get("confirmation_id"):
            return member
        async with async_session_factory() as child_db:
            await set_tenant_context(child_db, str(tenant_id))
            child_session = await child_db.scalar(
                select(ChatSession).where(
                    ChatSession.id == session.id, ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id
                )
            )
            try:
                await _authorize({"db": child_db, "tenant_id": tenant_id, "actor_id": user_id}, create=True)
                policy = await get_active_policy(child_db, tenant_id)
                card = member["card"]
                if (
                    action == "approve"
                    and not evaluate_tool_call(policy, card["tool_name"], card["tool_input"])["allowed"]
                ):
                    raise ValueError("Current policy blocks this correction.")
                errors = []
                child_db.info["accounting_group_execution"] = {
                    "group_approval_id": str(message.id),
                    "manifest_digest": so["tool_input"]["manifest_digest"],
                    "confirmation_id": member["confirmation_id"],
                    "card_digest": digest(card),
                    "session_id": str(session.id),
                    "tenant_id": str(tenant_id),
                    "action": action,
                }
                async with asyncio.timeout(150):
                    async for event in run_chat_turn(
                        db=child_db,
                        session=child_session,
                        user_message="",
                        user_id=user_id,
                        tenant_id=tenant_id,
                        write_confirm={"action": action, "confirmation_id": member["confirmation_id"]},
                    ):
                        if event.get("type") == "error":
                            errors.append(event.get("error", "Correction needs review."))
                reason = errors[0] if errors else None
            except Exception as exc:
                reason = f"Correction needs review ({type(exc).__name__}); check its persisted outcome before retrying."
                stop.set()
            finally:
                child_db.info.pop("accounting_group_execution", None)
            await child_db.rollback()
            await set_tenant_context(child_db, str(tenant_id))
            child_db.expire_all()
            child = await child_db.scalar(
                select(ChatMessage).where(
                    ChatMessage.id == uuid.UUID(member["confirmation_id"]),
                    ChatMessage.tenant_id == tenant_id,
                    ChatMessage.session_id == session.id,
                )
            )
            value = child.structured_output if child else member["card"]
            if value.get("status") in {"executing", "indeterminate"}:
                stop.set()
            await log_event(
                child_db,
                tenant_id,
                category="transaction_ops",
                action="accounting_group.case.completed",
                actor_id=user_id,
                resource_type="chat_message",
                resource_id=member["confirmation_id"],
                correlation_id=correlation_id,
                payload={
                    "group_approval_id": str(message.id),
                    "approved_by": str(user_id) if action == "approve" else None,
                    "case_id": member["case_id"],
                    "status": value.get("status"),
                    "reason": reason,
                    "verification": value.get("accounting_verification"),
                },
            )
            await child_db.commit()
            return {**member, "card": value, **({"reason": reason} if reason else {})}

    try:
        outcomes = await bounded_map(members, execute, stop=stop)
    except BaseException:
        # A timeout/disconnect is not evidence that a submitted write failed.
        # Preserve completed children, explicitly stop the batch and never retry.
        await asyncio.shield(persist_interrupted_group(tenant_id, session.id, message.id, so, user_id, correlation_id))
        raise
    verified = sum(
        (m.get("card", {}).get("accounting_verification") or {}).get("status") == "verified" for m in outcomes
    )
    eligible_count = len(so["tool_input"]["confirmation_ids"])
    rejected = sum(m.get("card", {}).get("status") == "rejected" for m in outcomes)
    status = (
        ("rejected" if rejected == eligible_count else "indeterminate")
        if action == "reject"
        else "approved"
        if verified == eligible_count
        else "indeterminate"
    )
    final = {**so, "status": status, "accounting_group": {**so["accounting_group"], "members": outcomes}}
    await set_tenant_context(db, str(tenant_id))
    message.structured_output = final
    note = (
        f"Verified {verified} of {eligible_count} approved invoice corrections. "
        "Each order retains its approval and execution audit. Full case and cash settlement remain separate."
        if action == "approve"
        else f"Rejected {rejected} of {eligible_count} proposed corrections."
        + (
            " Rejection is incomplete. Review each recorded outcome; no automatic retry."
            if rejected != eligible_count
            else ""
        )
    )
    message.content = note
    if action == "approve":
        # A bounded read-only recovery may finish an earlier child while this
        # batch is still draining. Render durable outcomes, not stale snapshots.
        from app.services.transaction_ops.accounting_recovery import refresh_group

        await db.flush()
        await refresh_group(db, tenant_id, session.id, message.id)
        final, note = message.structured_output, message.content
        status = final["status"]
        verified = sum(
            (m.get("card", {}).get("accounting_verification") or {}).get("status") == "verified"
            for m in final["accounting_group"]["members"]
        )
    await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action="accounting_group.completed",
        actor_id=user_id,
        resource_type="chat_message",
        resource_id=str(message.id),
        correlation_id=correlation_id,
        payload={
            "status": status,
            "verified": verified,
            "rejected": rejected,
            "eligible": eligible_count,
            "confirmation_ids": so["tool_input"]["confirmation_ids"],
        },
    )
    await db.commit()
    yield {
        "type": "message",
        "message": {
            "id": str(message.id),
            "role": "assistant",
            "content": note,
            "structured_output": final,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }


async def persist_interrupted_group(tenant_id, session_id, message_id, so, actor_id, correlation_id):
    async with async_session_factory() as db:
        await set_tenant_context(db, str(tenant_id))
        parent = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.id == message_id, ChatMessage.tenant_id == tenant_id, ChatMessage.session_id == session_id
            )
        )
        members = []
        for member in so["accounting_group"]["members"]:
            value = dict(member)
            if member.get("confirmation_id"):
                child = await db.scalar(
                    select(ChatMessage).where(
                        ChatMessage.id == uuid.UUID(member["confirmation_id"]),
                        ChatMessage.tenant_id == tenant_id,
                        ChatMessage.session_id == session_id,
                    )
                )
                value["card"] = child.structured_output if child else member["card"]
                value["reason"] = "Batch interrupted. Check this order’s recorded outcome; no automatic retry."
            members.append(value)
        parent.structured_output = {
            **so,
            "status": "indeterminate",
            "accounting_group": {**so["accounting_group"], "members": members},
        }
        parent.content = (
            "Group execution was interrupted. Review each recorded result before proposing further changes."
        )
        await log_event(
            db,
            tenant_id,
            category="transaction_ops",
            action="accounting_group.interrupted",
            actor_id=actor_id,
            resource_type="chat_message",
            resource_id=str(message_id),
            correlation_id=correlation_id,
            payload={"approved_by": str(actor_id), "confirmation_ids": so["tool_input"]["confirmation_ids"]},
            status="error",
        )
        await db.commit()
