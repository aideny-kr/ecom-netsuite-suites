"""Aggregate verified per-order results and publish one exact dependent group.

The parent row lock is held by refresh_group. A completed stage can publish its
successor only once. The successor still requires a new human approval.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.chat import ChatMessage
from app.services.audit_service import log_event
from app.services.transaction_ops.accounting_group import build_group_card


async def refresh(db, tenant_id, parent):
    so = parent.structured_output
    members = so["accounting_group"]["members"]
    eligible = [m for m in members if m.get("confirmation_id")]
    receipts = [(m.get("card") or {}).get("accounting_receipt") for m in eligible]
    latest = []
    for member, receipt in zip(eligible, receipts):
        current, predecessor = receipt, member["confirmation_id"]
        for _ in range(8):
            next_id = ((current or {}).get("next_step") or {}).get("confirmation_id")
            if not next_id:
                break
            child = await db.scalar(
                select(ChatMessage).where(
                    ChatMessage.tenant_id == tenant_id,
                    ChatMessage.session_id == parent.session_id,
                    ChatMessage.id == UUID(next_id),
                )
            )
            child_so = child.structured_output if child else {}
            if (
                child_so.get("accounting_plan_predecessor") != predecessor
                or (child_so.get("accounting_review") or {}).get("case_id") != member["case_id"]
                or not child_so.get("accounting_receipt")
            ):
                break
            current, predecessor = child_so["accounting_receipt"], next_id
        latest.append(current)
        if current:
            member["resolution_receipt"] = current
    complete = sum(bool(r) for r in receipts)
    reconciled = sum(bool(r and r["status"] == "reconciled") for r in latest)
    # Counts are derived from the members' own state every time, and say which members
    # were never prepared (and how many of those hit the preparation deadline) instead
    # of folding them into "orders to reconcile" — a summary that could never reach a
    # terminal state and read "7 of 54" while 30 children held reconciled receipts.
    unprepared = [m for m in members if not m.get("confirmation_id")]
    deadline = sum(m.get("preparation_status") == "incomplete" for m in unprepared)
    if reconciled == len(members):
        status = "reconciled"
    elif eligible and reconciled == len(eligible):
        status = "prepared_reconciled"
    else:
        status = "in_progress"
    progress = {
        **(so.get("accounting_plan_progress") or {}),
        "orders": len(members),
        "approved_orders": len(eligible),
        "prepared": len(eligible),
        "unprepared": len(unprepared),
        "deadline": deadline,
        "results_ready": complete,
        "reconciled": reconciled,
        "remaining": len(eligible) - reconciled,
        "status": status,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    parent.structured_output = {
        **so,
        "accounting_group": {**so["accounting_group"], "members": members},
        "accounting_plan_progress": progress,
    }
    if not complete:
        return
    parent.content = (
        f"{reconciled} of {len(eligible)} approved corrections reconciled; "
        f"{complete} of {len(eligible)} have full review results. "
        + (
            f"{len(unprepared)} of {len(members)} orders were not prepared"
            + (f" ({deadline} reached the preparation time limit)" if deadline else "")
            + " and still need a correction. "
            if unprepared
            else ""
        )
        + "Each order retains its record links, original approver and audit evidence."
    )
    # A failed or blocked sibling must not strand already prepared dependent cards.
    terminal = all(
        r
        or (m.get("card") or {}).get("status") in {"failed", "rejected", "indeterminate"}
        or ((m.get("card") or {}).get("accounting_completion") or {}).get("status") == "blocked"
        or (
            (so.get("accounting_group_dispatch") or {}).get("status") in {"finished", "interrupted"}
            and (m.get("card") or {}).get("status") == "pending"
            and not (m.get("card") or {}).get("accounting_execution")
        )
        for m, r in zip(eligible, receipts)
    )
    if not terminal:
        return
    published = set(progress.get("published_confirmation_ids") or [])
    following = []
    for member, receipt in zip(eligible, receipts):
        identifier = ((receipt or {}).get("next_step") or {}).get("confirmation_id")
        if not identifier or identifier in published:
            continue
        child = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.session_id == parent.session_id,
                ChatMessage.id == UUID(identifier),
            )
        )
        if (
            not child
            or child.structured_output.get("status") != "pending"
            or child.structured_output.get("accounting_plan_predecessor") != member["confirmation_id"]
        ):
            continue
        following.append(
            {
                "case_id": member["case_id"],
                "order_reference": member["order_reference"],
                "confirmation_id": identifier,
                "card": child.structured_output,
            }
        )
    if following:
        parent_id = uuid4()
        card = build_group_card(
            following, {"group_id": str(parent_id), "scope": so["accounting_group"]["scope"]}, str(parent.session_id)
        )
        db.add(
            ChatMessage(
                id=parent_id,
                tenant_id=tenant_id,
                session_id=parent.session_id,
                role="assistant",
                content=f"{len(following)} next corrections are ready. Review this exact group for approval.",
                structured_output={**card.model_dump(mode="json"), "accounting_plan_predecessor": str(parent.id)},
                token_count=0,
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        )
        progress["followup_confirmation_id"] = str(parent_id)
        progress["published_confirmation_ids"] = sorted(published | {m["confirmation_id"] for m in following})
        await log_event(
            db,
            tenant_id,
            "transaction_ops",
            "accounting_plan.group_followup_prepared",
            actor_type="system",
            resource_type="chat_message",
            resource_id=str(parent.id),
            payload={
                "confirmation_id": str(parent_id),
                "orders": len(following),
                "financial_writes": 0,
                "requires_new_human_approval": True,
                "manifest_digest": card.tool_input["manifest_digest"],
            },
        )
    # Terminal: every prepared correction has an outcome. Unprepared members keep the
    # summary from reading "reconciled", but they are not review work: they need a
    # correction prepared, which the status names apart from a sibling that failed.
    if reconciled == len(members):
        final = "reconciled"
    elif eligible and reconciled == len(eligible):
        final = "prepared_reconciled"
    else:
        final = "needs_review"
    progress.update(followup_published=True, status=final)
    parent.structured_output = {**parent.structured_output, "accounting_plan_progress": progress}
