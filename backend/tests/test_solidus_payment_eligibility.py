"""Failed header payments never become comparison or correction work."""

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.canonical import Order
from app.models.pipeline import CursorState
from app.services.ingestion import solidus_sync as sync
from app.services.transaction_ops import case_service, tax_correction
from app.services.transaction_ops.case_groups import list_groups
from app.services.transaction_ops.source_eligibility import exclusion_report, payment_failed
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_solidus_ingestion import NOW, connection, fake_pages, source_order
from tests.test_transaction_ops_runner import State, execute
from tests.test_transaction_ops_runner import source_order as envelope


@pytest.mark.parametrize("state", ["paid", "balance_due", "credit_owed", "pending", None])
async def test_failed_attempt_does_not_exclude_other_order_payment_states(state):
    source = envelope()
    source["orders"][0].update(payment_state=state, payments=[{"state": "failed"}])
    runner = State()
    result = await execute(runner, source=source)
    assert result["termination_reason"] == "done"
    assert "target" in runner.events
    assert runner.run.progress_json["excluded"] == 0
    assert not payment_failed(source["orders"][0])


async def test_failed_order_skips_netsuite_and_keeps_next_order_moving():
    from app.services.transaction_ops.runner import run_investigation

    runner = State()
    source = envelope()
    failed_ref = source["orders"][0]["number"]
    source["orders"][0]["payment_state"] = "failed"
    paid = envelope()
    paid["orders"][0].update(id="2", number="R100000002", payment_state="paid")
    runner.run.params_json["order_references"].append("R100000002")
    from tests.test_transaction_ops_runner import NOW as RUN_NOW
    from tests.test_transaction_ops_runner import missing_target

    target = AsyncMock(return_value=missing_target())
    result = await run_investigation(
        None,
        runner.tenant,
        runner.run_id,
        _state=runner,
        _source_reader=AsyncMock(side_effect=[source, paid]),
        _target_reader=target,
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: RUN_NOW,
    )
    assert result["termination_reason"] == "done"
    assert target.await_count == 1 and target.await_args.args[5] == "R100000002"
    assert runner.run.progress_json["excluded"] == 1
    assert runner.run.progress_json["processed"] == 1
    assert runner.run.progress_json["pending_refs"] == []
    assert runner.reports[failed_ref]["balance"]["status"] == "excluded"
    assert runner.reports[failed_ref]["balance"]["amounts"] == {}


async def test_fresh_failed_state_blocks_shared_correction_preflight(monkeypatch):
    source = envelope()
    source["orders"][0]["payment_state"] = "failed"
    read = AsyncMock(return_value=source)
    monkeypatch.setattr(tax_correction, "read_framework_order", read)
    with pytest.raises(SourceReadError, match="source_payment_failed"):
        await tax_correction.refresh_source(None, uuid4(), {}, source["orders"][0]["number"])
    source["orders"][0].update(payment_state="paid", payments=[{"state": "failed"}])
    assert (await tax_correction.refresh_source(None, uuid4(), {}, source["orders"][0]["number"]))[
        "payment_state"
    ] == "paid"


async def test_failed_only_page_advances_raw_cursor_without_importing_failed_orders(db, admin_user, monkeypatch):
    actor = admin_user[0]
    conn = await connection(db, actor.tenant_id)
    orders = [source_order(f"R10012{i:004}", payment_state="failed" if i < 20 else "paid") for i in range(21)]
    orders[0]["total"] = None  # An excluded checkout need not have complete monetary evidence.
    calls = fake_pages(monkeypatch, orders)
    first = await sync.sync_solidus_orders(db, actor.tenant_id, conn.id, now=NOW, max_pages=1)
    assert not first["complete"] and first["records_synced"] == 0
    cursor = await db.scalar(select(CursorState).where(CursorState.connection_id == conn.id))
    import json

    assert json.loads(cursor.cursor_value)["last_source_id"] == orders[19]["id"]
    second = await sync.sync_solidus_orders(db, actor.tenant_id, conn.id, now=NOW, max_pages=1)
    assert second["complete"] and second["records_synced"] == 1
    assert calls[-1]["after_id"] == int(orders[19]["id"])
    rows = (await db.scalars(select(Order).where(Order.tenant_id == actor.tenant_id))).all()
    assert [row.order_number for row in rows] == [orders[20]["number"]]
    audits = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id, AuditEvent.action == "solidus.orders.page"
            )
        )
    ).all()
    assert any(len(row.payload["excluded_payment_failed"]) == 20 for row in audits)


async def test_observed_failed_order_is_hidden_preserves_history_and_can_become_paid(db, admin_user):
    from app.services.table_service import export_table_csv, query_table

    actor = admin_user[0]
    conn = await connection(db, actor.tenant_id, metadata_json={"api_profile": "framework_sync"})
    paid = source_order(payment_state="paid")
    await sync.save_observed_order(db, actor.tenant_id, conn.id, paid, NOW)
    failed = source_order(payment_state="failed", updated_at=(NOW + timedelta(seconds=1)).isoformat())
    await sync.save_observed_order(db, actor.tenant_id, conn.id, failed, NOW + timedelta(seconds=1))
    rows = (await db.scalars(select(Order).where(Order.tenant_id == actor.tenant_id))).all()
    assert len(rows) == 1 and rows[0].raw_data["order"]["payment_state"] == "failed"
    assert (await query_table(db, "orders", tenant_id=actor.tenant_id))["total"] == 0
    assert paid["number"] not in await export_table_csv(db, "orders", tenant_id=actor.tenant_id)
    # An older response must not replace a newer failed observation.
    await sync.save_observed_order(db, actor.tenant_id, conn.id, paid, NOW + timedelta(seconds=2))
    assert (await query_table(db, "orders", tenant_id=actor.tenant_id))["total"] == 0
    paid["updated_at"] = (NOW + timedelta(seconds=3)).isoformat()
    await sync.save_observed_order(db, actor.tenant_id, conn.id, paid, NOW + timedelta(seconds=3))
    assert (await query_table(db, "orders", tenant_id=actor.tenant_id))["total"] == 1


async def test_exclusion_retires_existing_case_without_claiming_reconciled_and_retains_audit(db, admin_user):
    from tests.test_transaction_cases import NOW as CASE_NOW
    from tests.test_transaction_cases import REF, observe, report
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, config, report(), CASE_NOW)
    case = (await case_service.list_cases(db, actor.tenant_id))[0]
    source = envelope()
    source["orders"][0].update(number=REF, payment_state="failed")
    source["read_at"] = (CASE_NOW + timedelta(seconds=1)).isoformat()
    await observe(db, actor, config, exclusion_report(source), CASE_NOW + timedelta(seconds=1))
    assert not await case_service.list_cases(db, actor.tenant_id)
    assert not (await list_groups(db, actor.tenant_id))["groups"]
    assert (await case_service.get_case(db, actor.tenant_id, case.id)).status == "open"
    assert len(await case_service.list_observations(db, actor.tenant_id, case.id)) == 2
    logs = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id, AuditEvent.action == "transaction_ops.source.excluded"
            )
        )
    ).all()
    assert len(logs) == 1 and logs[0].payload["reason"] == "source_payment_failed"
    await observe(db, actor, config, report(observed=CASE_NOW + timedelta(seconds=2)), CASE_NOW + timedelta(seconds=2))
    assert (await case_service.list_cases(db, actor.tenant_id))[0].id == case.id
    # A late response from the earlier failed-payment read cannot retire the
    # subsequently paid order again.
    await observe(db, actor, config, exclusion_report(source), CASE_NOW + timedelta(seconds=3))
    assert (await case_service.list_cases(db, actor.tenant_id))[0].id == case.id


async def test_period_exclusion_supersedes_old_variance_and_can_be_replaced_by_paid_evidence(
    db, admin_user, monkeypatch
):
    from app.services.transaction_ops.period_review import review_results
    from tests.test_transaction_review_results import evidence, recheck
    from tests.test_transaction_review_slices import review

    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    old = await evidence(db, actor, root, "R123456789", "difference", root.created_at)
    later = await recheck(db, actor, root)
    failed = await evidence(db, actor, later, "R123456789", "excluded", root.created_at + timedelta(seconds=1))
    failed.report_json = {
        **failed.report_json,
        "source_eligibility": {"eligible": False, "reason": "source_payment_failed"},
    }
    # Explicitly date the JSON update; the ORM's onupdate clock otherwise
    # replaces the synthetic timestamp and can tie the original observation.
    failed.updated_at = root.created_at + timedelta(seconds=2)
    await db.flush()
    result = await review_results(db, actor.tenant_id, root.id)
    assert result["total"] == 0 and result["summary"]["checked"] == 0
    assert old.report_json["balance"]["status"] == "difference"
    paid_run = await recheck(db, actor, root)
    await evidence(db, actor, paid_run, "R123456789", "matched", root.created_at + timedelta(seconds=3))
    assert (await review_results(db, actor.tenant_id, root.id))["summary"]["matched"] == 1
