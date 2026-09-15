from copy import deepcopy

import pytest

from app.services.transaction_ops.resolution_plan import operation_identity, proposal_plan
from tests.test_accounting_approval_flow import kind_proposal


@pytest.mark.parametrize("kind", ["tax", "discount", "credit", "sales_order"])
def test_plan_keeps_each_required_stage_and_human_approval(kind):
    p = kind_proposal(kind)
    plan = proposal_plan(p, {})
    assert [s["id"] for s in plan["steps"]] == ["posting", "sales_order", "reconcile"]
    assert plan["approval"] == {"mode": "human", "policy_id": None, "automatic_approval_enabled": False}
    assert plan["steps"][2]["status"] == "waiting"
    assert plan["steps"][1]["affects_gl"] is False
    if kind == "sales_order":
        assert plan["steps"][0]["status"] == "verified"
        assert plan["steps"][1]["status"] == "awaiting_approval"
    else:
        assert plan["steps"][0]["status"] == "awaiting_approval"
        assert plan["steps"][1]["status"] == "waiting"
    if kind == "credit":
        assert plan["steps"][0]["record_id"] is None, "An invoice ID is not the future credit memo ID"


def test_operation_identity_ignores_observation_and_chat_but_binds_money_scope_rules():
    p = kind_proposal("sales_order")
    q = deepcopy(p)
    q.update(observed_at="later", session_id="different")
    q["before"]["lastModifiedDate"] = "later"
    assert operation_identity(q) == operation_identity(p)
    for section, key, value in [
        ("scope", "subsidiary_id", "999"),
        ("profile", "item_id", "999"),
        ("proposed_fields", "discountRate", -5.01),
        ("expected_after", "total", "94.99"),
        ("source", "total", "94.99"),
    ]:
        q = deepcopy(p)
        q[section][key] = value
        assert operation_identity(q) != operation_identity(p)
    q = deepcopy(p)
    q["tenant_id"] = "other"
    assert operation_identity(q) != operation_identity(p)


@pytest.mark.parametrize(
    "state,preflight_failed,blocked",
    [
        ("executing", False, True),
        ("indeterminate", False, True),
        ("approved", False, True),
        ("failed", False, True),
        ("failed", True, False),
    ],
)
async def test_duplicate_intents_are_durable_across_messages(db, admin_user, state, preflight_failed, blocked):
    from uuid import uuid4

    from app.models.chat import ChatMessage, ChatSession
    from app.services.audit_service import log_event
    from app.services.transaction_ops.resolution_plan import previous_execution

    actor = admin_user[0]
    p = kind_proposal("sales_order")
    p["tenant_id"] = str(actor.tenant_id)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id)
    db.add(session)
    await db.flush()
    old = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={
            "status": state,
            "accounting_execution": {"operation_key": operation_identity(p), "approved_by": str(actor.id)},
        },
    )
    db.add(old)
    await db.flush()
    if preflight_failed:
        await log_event(
            db,
            actor.tenant_id,
            "transaction_ops",
            "accounting_correction.precondition_failed",
            actor_id=actor.id,
            resource_type="chat_message",
            resource_id=str(old.id),
            payload={"approved_by": str(actor.id), "financial_writes": 0},
        )
        await db.flush()
    prior = await previous_execution(db, actor.tenant_id, uuid4(), p)
    assert bool(prior) == blocked
    assert await previous_execution(db, actor.tenant_id, old.id, p) is None
    p["proposed_fields"]["discountRate"] = -6
    assert await previous_execution(db, actor.tenant_id, uuid4(), p) is None
