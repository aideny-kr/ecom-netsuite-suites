"""Direct-service-call coverage for the extracted approve_group_core — proves
the endpoint's behavior lives in the service, callable with no FastAPI user
object (plain tenant_id/actor_id kwargs)."""

from decimal import Decimal

from app.api.v1.reconciliation import plan_resolutions
from app.services.reconciliation.group_actions import approve_group_core
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def test_approve_group_core_direct_call_no_fastapi_user(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
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
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)

    out = await approve_group_core(
        db,
        tenant_id=tenant_a.id,
        actor_id=user.id,
        run_id=str(run.id),
        group_key="fees:book_fee_line:deposit",
        notes="direct call",
        included_above_materiality_ids=[],
        excluded_ids=[],
        currency=None,
    )
    assert out.approved_count == 1
    assert out.skipped_count == 0
    assert out.run_id == str(run.id)
    assert out.group_key == "fees:book_fee_line:deposit"
