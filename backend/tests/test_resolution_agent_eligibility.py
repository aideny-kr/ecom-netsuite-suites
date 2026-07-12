from decimal import Decimal

from app.services.feature_flag_service import DEFAULT_FLAGS
from app.services.reconciliation.resolution_agent import (
    AGENT_ALLOWED_ACTIONS,
    MAX_ITEMS_PER_RUN,
    fetch_agent_eligible,
)
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


def test_flag_registered_default_off():
    assert DEFAULT_FLAGS.get("recon_resolution_agent") is False


def test_agent_action_policy():
    assert AGENT_ALLOWED_ACTIONS == frozenset(
        {"book_fee_line", "create_and_apply_deposit", "apply_deposit", "writeoff_je", "carry_forward", "needs_human"}
    )
    assert "credit_memo_refund" not in AGENT_ALLOWED_ACTIONS
    assert "void_duplicate" not in AGENT_ALLOWED_ACTIONS
    assert MAX_ITEMS_PER_RUN == 50


async def test_fetch_agent_eligible_filters(db, tenant_a):
    from app.api.v1.reconciliation import plan_resolutions

    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    # one manual_adjustment (eligible after planning) + one fee (not needs_human)
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="manual_adjustment",
        variance_amount=Decimal("77.10"),
        stripe_amount=Decimal("500.00"),
        netsuite_amount=Decimal("422.90"),
        evidence={"charge_source_id": "ch_m", "order_reference": "R9"},
    )
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_f", "order_reference": "R1"},
    )
    await db.flush()
    # enable the UI flag for the mutation endpoint (create_test_tenant already
    # seeds all DEFAULT_FLAGS rows disabled, so upsert via the shared helper
    # rather than a raw insert to avoid a unique-constraint duplicate).
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui", True)
    await plan_resolutions(str(run.id), user=user, db=db)

    eligible = await fetch_agent_eligible(db, tenant_a.id, run.id)
    assert len(eligible) == 1
    assert eligible[0].action == "needs_human"
    assert eligible[0].source == "planner"
