"""Exact group proposals and bounded execution through the existing signed HITL path.

Group IDs select evidence, never authorize writes. The parent signs a frozen
manifest of individually signed child cards; each child keeps its own CAS,
fresh preconditions, external-call audit and independent invoice/GL proof.
"""

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.core.database import async_session_factory, engine, set_tenant_context
from app.models.chat import ChatMessage
from app.services.audit_service import log_event
from app.services.chat.write_confirmation_service import (
    WriteConfirmationPayload,
    mint_confirmation_token,
    validate_and_extract_confirmation,
)
from app.services.transaction_ops.treatments import collision_key

CONCURRENCY = 3


class AccountingCapacityBusyError(ValueError):
    code = "accounting_capacity_busy"


PREPARATION_TIMEOUT = 450  # Leave time to publish an explicit result within the chat budget.
MAX_GROUP_BYTES = 4 * 1024 * 1024


def require_bounded_group(group):
    if len(json.dumps(group, separators=(",", ":"), default=str).encode()) > MAX_GROUP_BYTES:
        raise ValueError("The group exceeds the approval review size limit; narrow the selected period or orders.")


GROUP_TOOL = "transaction_ops_accounting_group_apply"  # Not exposed to model/MCP dispatch.
_authorization_session_factory = async_session_factory


def preparation_timing(members, wall_ms):
    """What the preparation cost: the wall clock, how many members were prepared, skipped
    or left at the deadline, and the per-member spread, so the cap can be tuned from
    measured cost instead of a guess."""
    totals = sorted(m["timing"]["total_ms"] for m in members if (m.get("timing") or {}).get("total_ms") is not None)
    return {
        "wall_ms": wall_ms,
        "concurrency": CONCURRENCY,
        "timeout_s": PREPARATION_TIMEOUT,
        "prepared": sum(1 for m in members if m.get("card")),
        "skipped": sum(1 for m in members if not m.get("card") and m.get("preparation_status") != "incomplete"),
        "deadline": sum(1 for m in members if m.get("preparation_status") == "incomplete"),
        "member_ms_max": totals[-1] if totals else None,
        "member_ms_median": totals[len(totals) // 2] if totals else None,
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def bounded_map(items, function, *, stop=None, timeout=None, unfinished=None):
    """Only three workers; no task per member, no shared AsyncSession."""
    if (timeout is None) != (unfinished is None):
        raise ValueError("A preparation deadline requires an explicit unfinished result.")
    if not items:
        return []
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
        if timeout is None:
            await asyncio.gather(*tasks)
        else:
            # Preparation is read-only. Preserve completed proposals when one
            # slow member exhausts its budget; never use this for posting.
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            output = [value if value is not None else unfinished(items[i]) for i, value in enumerate(output)]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return output


def build_group_card(members, selection, session_id):
    """Sign the same exact manifest for initial and dependent group stages."""
    eligible = [m for m in members if m.get("card")]
    targets = [
        (
            m["card"]["accounting_review"]["scope"]["netsuite_account_id"],
            m["card"]["accounting_review"].get("lock_record_type", m["card"]["record_type"]),
            collision_key(m["card"]["accounting_review"])[1],
        )
        for m in eligible
    ]
    if len(set(targets)) != len(targets):
        raise ValueError("Multiple cases target the same invoice; separate their identities before group approval.")
    from app.services.transaction_ops.accounting_treatments import investigation_batches, treatment_batches

    group = {
        "group_id": selection["group_id"],
        "scope": selection["scope"],
        "members": members,
        "concurrency": CONCURRENCY,
        "treatment_batches": treatment_batches(members),
        "investigation_batches": investigation_batches(members),
    }
    require_bounded_group(group)
    params = {"manifest_digest": digest(group), "confirmation_ids": [m["confirmation_id"] for m in eligible]}
    card = WriteConfirmationPayload(
        mutation_type="execute",
        record_type="accounting corrections",
        proposed_fields={"eligible_orders": len(eligible)},
        tool_name=GROUP_TOOL,
        tool_input=params,
        accounting_group=group,
        confirmation_token=mint_confirmation_token(GROUP_TOOL, params, [], session_id),
        invariant_errors=[] if eligible else ["No supported corrections are ready for approval."],
    )
    return card


async def prepare_group_confirmation(*, db, tenant_id, actor_id, correlation_id, session_id, tools, policy, **_):
    from app.mcp.tools.transaction_ops_tools import execute_accounting_evidence
    from app.services.transaction_ops.group_investigation import handoff, summarize
    from app.services.transaction_ops.read_batch import reference_read_batch
    from app.services.transaction_ops.tax_correction import candidate_confirmation

    selection = db.info.pop("accounting_group_selection", None)
    if not selection:
        raise ValueError("Refresh the scoped issue group before preparing corrections.")

    previous = db.info.get("accounting_group_investigation")
    if previous and previous.get("scope") == selection["scope"] and previous.get("group_id") == selection["group_id"]:
        return None

    async def prepare(member):
        # Per-member timing travels on the member (into the parent card and the audits):
        # the run that lost 23 of 54 members to the 450 s cap had no per-case cost at all.
        started = time.monotonic()
        timing = {}

        def elapsed():
            return int((time.monotonic() - started) * 1000)

        async with async_session_factory() as child_db:
            await set_tenant_context(child_db, str(tenant_id))
            context = dict(
                db=child_db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                session_id=session_id,
            )
            routes = []
            investigation_evidence = {}
            try:
                async with asyncio.timeout(120):
                    evidence = await execute_accounting_evidence(
                        {"case_id": member["case_id"]}, context={**context, "group_preparation": True}
                    )
                    timing["evidence_ms"] = elapsed()
                    collected = evidence.get("accounting_evidence") or {}
                    routes = collected.get("investigation_routes", [])
                    investigation_evidence = summarize(collected)
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
                        timing["total_ms"] = elapsed()
                        return {**member, "confirmation_id": str(uuid.uuid4()), "card": value, "timing": timing}
                    reason = (
                        "Solution identified. Account configuration, native preview and approval are still required."
                        if collected.get("resolution_intents")
                        else "No validated correction is ready. Continue investigation using the recorded evidence."
                    )
            except Exception as exc:
                await child_db.rollback()
                await set_tenant_context(child_db, str(tenant_id))
                reason = f"Preparation needs review ({type(exc).__name__})."
            timing["total_ms"] = elapsed()
            await log_event(
                child_db,
                tenant_id,
                category="transaction_ops",
                action="accounting_group.case.skipped",
                actor_id=actor_id,
                resource_type="transaction_case",
                resource_id=member["case_id"],
                correlation_id=correlation_id,
                payload={"reason": reason, "investigation_routes": routes, "timing": timing, "financial_writes": 0},
            )
            await child_db.commit()
            return {
                **member,
                "reason": reason,
                "investigation_routes": routes,
                "investigation_evidence": investigation_evidence,
                "timing": timing,
            }

    preparation_started = time.monotonic()
    try:
        with reference_read_batch() as reads:
            members = await bounded_map(
                selection["members"],
                prepare,
                timeout=PREPARATION_TIMEOUT,
                unfinished=lambda member: {
                    **member,
                    "preparation_status": "incomplete",
                    "reason": "Preparation time limit reached. No correction was submitted for this order; "
                    "continue preparation from fresh evidence.",
                    "investigation_routes": [
                        {
                            "code": "preparation_incomplete",
                            "next_step": "Resume preparation for this order; completed approvals remain available.",
                        }
                    ],
                },
            )
    except (asyncio.CancelledError, TimeoutError):
        await asyncio.shield(record_preparation_interrupted(tenant_id, actor_id, session_id, correlation_id, selection))
        raise
    if not any(member.get("card") for member in members):
        investigation = handoff(selection, members, reads.hits)
        db.info["accounting_group_investigation"] = investigation
        await log_event(
            db,
            tenant_id,
            category="transaction_ops",
            action="accounting_group.investigation_required",
            actor_id=actor_id,
            resource_type="chat_session",
            resource_id=session_id,
            correlation_id=correlation_id,
            payload=investigation,
        )
        return None
    card = build_group_card(members, selection, session_id)
    group = card.accounting_group
    params = card.tool_input
    eligible = [m for m in members if m.get("card")]
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
            "treatment_batches": group["treatment_batches"],
            "investigation_batches": group["investigation_batches"],
            "timing": preparation_timing(members, int((time.monotonic() - preparation_started) * 1000)),
            "financial_writes": 0,
        },
    )
    return card, (
        f"Prepared {len(eligible)} exact accounting corrections across {len(group['treatment_batches'])} "
        f"validated treatments for {len(members)} orders. Remaining cases retain shared investigation steps."
    )


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
    # Celery owns disposable event-loop-local engines. Never borrow the app's
    # global pool from a worker loop.
    factory = getattr(db, "info", {}).get("accounting_authorization_session_factory", _authorization_session_factory)
    async with factory() as auth_db:
        await set_tenant_context(auth_db, str(tenant_id))
        await _authorize({"db": auth_db, "tenant_id": tenant_id, "actor_id": actor_id}, create=True, fresh=True)
        policy = await get_active_policy(auth_db, tenant_id)
        if not evaluate_tool_call(policy, tool_name, tool_input)["allowed"]:
            raise ValueError("Current policy blocks this correction. No update was sent.")


def validate_manifest(so, session_id):
    valid, name, params = validate_and_extract_confirmation(so, session_id)
    group = so.get("accounting_group") or {}
    require_bounded_group(group)
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
        # collision_key refuses with TreatmentError (a ValueError) when the member has no
        # lock document; build_group_card gets the same refusal from the same call.
        targets.append(
            (p["scope"]["netsuite_account_id"], p.get("lock_record_type", card["record_type"]), collision_key(p)[1])
        )
    if len(set(targets)) != len(targets):
        raise ValueError("Overlapping document corrections cannot be approved together.")
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
            record_type, record_id = collision_key(proposal)
            record = key(f"accounting-write:{account}:{record_type}:{record_id}")
            if not await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": record}):
                raise ValueError("Another approved correction is checking this invoice. No additional update was sent.")
            keys.append(record)
            for index in range(CONCURRENCY):
                slot = key(f"accounting-write:{account}:slot:{index}")
                if await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": slot}):
                    keys.append(slot)
                    break
            else:
                raise AccountingCapacityBusyError(
                    "Three corrections are already running for this account. This update was not sent."
                )
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
    from app.services.chat.orchestrator import _cas_claim_write_confirmation

    if action not in {"approve", "reject"}:
        raise ValueError("Unsupported group decision.")
    await _authorize({"db": db, "tenant_id": tenant_id, "actor_id": user_id}, create=True, fresh=True)
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
    from app.services.transaction_ops.accounting_dispatch import accept_dispatch, publish

    so = await accept_dispatch(db, tenant_id, session, message, so, action, user_id, correlation_id)
    note = (
        f"Group {'approval' if action == 'approve' else 'rejection'} accepted. "
        "Processing each order in the background; you can leave this page."
    )
    if not await _cas_claim_write_confirmation(db, message, so, "executing", content=note):
        await db.rollback()
        raise ValueError("This group approval was already claimed.")
    # The outbox and approval audit committed atomically. A broker failure does
    # not lose the job: the existing action collector republishes durable work.
    await publish(tenant_id, message.id)
    yield {
        "type": "message",
        "message": {
            "id": str(message.id),
            "role": "assistant",
            "content": note,
            "structured_output": {**so, "status": "executing"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }
