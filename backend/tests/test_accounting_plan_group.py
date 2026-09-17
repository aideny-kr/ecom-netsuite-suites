from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.chat import ChatMessage, ChatSession
from app.services.transaction_ops import accounting_plan_group
from app.services.transaction_ops.accounting_group import validate_manifest
from tests.test_accounting_group import group_fixture


async def test_group_prepares_exact_followup_once_and_late_recovery_is_not_stranded(db, admin_user):
    actor = admin_user[0]
    so, fake_session = group_fixture(2)
    session = ChatSession(id=fake_session.id, tenant_id=actor.tenant_id, user_id=actor.id)
    db.add(session)
    await db.flush()
    so["accounting_group"]["scope"] = so["accounting_group"]["members"][0]["card"]["accounting_review"]["scope"]
    parent = ChatMessage(
        tenant_id=actor.tenant_id, session_id=session.id, role="assistant", content="", structured_output=so
    )
    db.add(parent)
    await db.flush()
    new_ids = []
    for member in so["accounting_group"]["members"]:
        child_id = uuid4()
        new_ids.append(child_id)
        next_so = {**deepcopy(member["card"]), "accounting_plan_predecessor": member["confirmation_id"]}
        db.add(
            ChatMessage(
                id=child_id,
                tenant_id=actor.tenant_id,
                session_id=session.id,
                role="assistant",
                content="",
                structured_output=next_so,
            )
        )
        member["card"].update(
            status="approved",
            accounting_receipt={"status": "partially_resolved", "next_step": {"confirmation_id": str(child_id)}},
        )
    # One sibling has an unknown outcome: it cannot block the other order forever.
    late = so["accounting_group"]["members"][1]["card"].pop("accounting_receipt")
    so["accounting_group"]["members"][1]["card"]["status"] = "indeterminate"
    parent.structured_output = deepcopy(so)
    await db.flush()
    await accounting_plan_group.refresh(db, actor.tenant_id, parent)
    await db.flush()
    first_id = parent.structured_output["accounting_plan_progress"]["followup_confirmation_id"]
    first = await db.get(ChatMessage, UUID(first_id))
    assert first.structured_output["status"] == "pending"
    assert len(validate_manifest(first.structured_output, str(session.id))) == 1
    assert first.structured_output["accounting_group"]["concurrency"] == 3
    await accounting_plan_group.refresh(db, actor.tenant_id, parent)
    assert parent.structured_output["accounting_plan_progress"]["followup_confirmation_id"] == first_id
    # The late read-only recovery eventually finishes and prepares another exact approval.
    updated = deepcopy(parent.structured_output)
    updated["accounting_group"]["members"][1]["card"].update(status="approved", accounting_receipt=late)
    parent.structured_output = updated
    await accounting_plan_group.refresh(db, actor.tenant_id, parent)
    await db.flush()
    second_id = parent.structured_output["accounting_plan_progress"]["followup_confirmation_id"]
    assert first_id != second_id
    second = await db.get(ChatMessage, UUID(second_id))
    assert len(validate_manifest(second.structured_output, str(session.id))) == 1
    assert second.structured_output["status"] == "pending"
    cards = list(await db.scalars(select(ChatMessage).where(ChatMessage.session_id == session.id)))
    assert len(cards) == 5, "One original group, two hidden children and two separately approved stages"
    # The original batch follows later verified outcomes without rewriting old approvals.
    resolved = await db.get(ChatMessage, new_ids[0])
    resolved.structured_output = {
        **resolved.structured_output,
        "accounting_receipt": {"status": "reconciled", "next_step": {"status": "complete"}},
    }
    await db.flush()
    await accounting_plan_group.refresh(db, actor.tenant_id, parent)
    assert parent.structured_output["accounting_plan_progress"]["reconciled"] == 1
    original_member = parent.structured_output["accounting_group"]["members"][0]
    assert original_member["card"]["accounting_receipt"]["status"] == "partially_resolved"
    assert original_member["resolution_receipt"]["status"] == "reconciled"


async def test_the_group_summary_counts_prepared_and_unprepared_members_separately(db, admin_user):
    """54 selected, 31 prepared, 30 reconciled read as '7 of 54' because the summary counted
    every member as an order to reconcile and was only refreshed by whoever ran last. The
    summary now says how many of the PREPARED corrections reconciled, how many members were
    never prepared (and why), and when it was computed."""
    actor = admin_user[0]
    so, fake_session = group_fixture(2)
    session = ChatSession(id=fake_session.id, tenant_id=actor.tenant_id, user_id=actor.id)
    db.add(session)
    await db.flush()
    so["accounting_group"]["scope"] = so["accounting_group"]["members"][0]["card"]["accounting_review"]["scope"]
    for member in so["accounting_group"]["members"]:
        member["card"].update(
            status="approved", accounting_receipt={"status": "reconciled", "next_step": {"status": "complete"}}
        )
    so["accounting_group"]["members"].append(
        {
            "case_id": str(uuid4()),
            "order_reference": "R000000999",
            "preparation_status": "incomplete",
            "reason": "Preparation time limit reached. No correction was submitted for this order.",
            "investigation_routes": [{"code": "preparation_incomplete", "next_step": "Resume preparation"}],
        }
    )
    parent = ChatMessage(
        tenant_id=actor.tenant_id, session_id=session.id, role="assistant", content="", structured_output=so
    )
    db.add(parent)
    await db.flush()
    await accounting_plan_group.refresh(db, actor.tenant_id, parent)
    progress = parent.structured_output["accounting_plan_progress"]
    assert progress["orders"] == 3 and progress["prepared"] == 2 and progress["unprepared"] == 1
    assert progress["deadline"] == 1 and progress["results_ready"] == 2 and progress["reconciled"] == 2
    assert progress["remaining"] == 0 and progress["status"] == "prepared_reconciled"
    assert progress["computed_at"]
    assert "2 of 2 approved corrections reconciled" in parent.content
    assert "1 of 3 orders" in parent.content and "not prepared" in parent.content
