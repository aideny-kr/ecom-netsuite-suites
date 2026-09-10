from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import state_service
from app.services.transaction_ops.runner import run_investigation
from tests.test_transaction_ops_runner import NOW, REF, State, missing_target, source_order


@pytest.mark.parametrize("already_seen", [False, True])
async def test_refund_window_includes_older_orders_and_does_not_process_duplicates(already_seen):
    state = State(window=True)
    state.run.config_snapshot["mapping_json"]["solidus_refund_step_id"] = str(uuid4())
    source = source_order()
    if not already_seen:
        source["orders"][0]["updated_at"] = (NOW - timedelta(days=100)).isoformat()
    page = AsyncMock(
        return_value={
            "page_complete": True,
            "page": 1,
            "total_count": 1 if already_seen else 0,
            "orders": source["orders"] if already_seen else [],
            "next_page": None,
        }
    )
    refund_page = AsyncMock(
        return_value={"page_complete": True, "orders": [{"id": "1", "number": REF}], "next_after_id": None}
    )
    native = AsyncMock(return_value=missing_target())
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _enabled=AsyncMock(return_value=True),
        _page_reader=page,
        _refund_page_reader=refund_page,
        _source_reader=AsyncMock(return_value=source),
        _target_reader=native,
        _source_refunds_reader=AsyncMock(
            return_value={"complete": True, "order_reference": REF, "currency": "USD", "amount": "0"}
        ),
    )
    assert result["termination_reason"] == "done"
    assert state.run.progress_json["refund_scan_complete"] is True
    assert state.run.progress_json["processed"] == 1
    assert REF in state.reports
    native.assert_awaited_once()


async def test_reference_deduplication_is_tenant_scoped_and_follows_continuations(db, admin_user, admin_user_b):
    from datetime import datetime, timezone

    from app.schemas.transaction_runs import RunCreate
    from tests.test_order_table_evidence import finding
    from tests.test_transaction_tables import seed_orders

    user, _ = admin_user
    order = (await seed_orders(db, user.tenant_id, [REF]))[0]
    _, evidence = await finding(db, user, order)
    run = await state_service.get_run(db, user.tenant_id, evidence.run_id)
    run.status, run.termination_reason = "finished", "budget"
    run.finished_at = datetime.now(timezone.utc)
    run.progress_json = {"processed": 1, "scan_count": 0, "pending_refs": [], "scan_complete": True}
    await db.flush()
    child = await state_service.create_run(
        db,
        user.tenant_id,
        run.config_id,
        RunCreate(evaluation_key=f"continue:{run.id}:2", order_references=(REF,)),
        actor=user,
        resume_from_run_id=run.id,
        automatic_continuation=True,
    )
    assert await state_service.unseen_references(db, user.tenant_id, child.id, [REF, "R000000002"]) == ["R000000002"]
    with pytest.raises(state_service.StateError, match="not_found"):
        await state_service.unseen_references(db, admin_user_b[0].tenant_id, child.id, [REF])
