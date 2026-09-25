from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models.transaction_ops import TransactionCaseObservation, TransactionFinding
from app.models.transaction_source_snapshot import TransactionSourceSnapshot
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import finding_batch, source_validation, staged_netsuite
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.runner import run_investigation
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_runner import missing_target
from tests.test_transaction_ops_state import config_input
from tests.test_transaction_ops_state_db import seed_config
from tests.test_transaction_source_snapshot import seed


async def setup(db, actor, monkeypatch, *, max_orders=100, size=10, action_mode="detect_only"):
    conn, evidence = await seed(db, actor.tenant_id)
    config = await seed_config(
        db,
        actor.tenant_id,
        actor,
        subsidiary_id="1",
        netsuite_account_id="6738075",
        max_api_calls=500,
        max_orders=max_orders,
        mapping_json={
            "reference_field": "tranid",
            "currency_minor_units": {"USD": 2},
            "business_entity_subsidiaries": {"legacy": "1"},
            "action_mode": action_mode,
        },
    )
    config = await state.create_config(
        db,
        actor.tenant_id,
        config_input(
            source_step_id=None,
            source_connection_id=conn.id,
            netsuite_connection_id=config.netsuite_connection_id,
            subsidiary_id="1",
            netsuite_account_id="6738075",
            mapping_json=config.mapping_json,
            max_api_calls=500,
            max_orders=max_orders,
        ),
        actor=actor,
    )
    now = datetime.now(timezone.utc)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(
            evaluation_key="chunk-runner",
            window_start=now - timedelta(days=1),
            window_end=now,
        ),
        actor=actor,
    )
    refs = [f"R1000000{i:02}" for i in range(size)]
    run.progress_json = dict(
        pending_refs=refs, scan_complete=True, refund_scan_complete=True, destination_scan_complete=True, phase="orders"
    )
    await db.commit()
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    events = []

    async def source(db, tenant, step, ref, **kwargs):
        events.append(("read", ref))
        result = deepcopy(evidence)
        result["read_at"] = datetime.now(timezone.utc).isoformat()
        result["orders"][0].update(number=ref, id=str(refs.index(ref) + 1))
        return result

    reader = AsyncMock(side_effect=source)
    monkeypatch.setattr(source_validation, "read_validated_order", reader)

    class Target:
        def __init__(self, *args, **kwargs):
            pass

        async def order(self, reference, **kwargs):
            events.append(("compare", reference))
            target = missing_target()
            target["observed_at"] = datetime.now(timezone.utc).isoformat()
            return target

    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", Target)
    original = state.record_finding_batch

    async def record(*args, **kwargs):
        events.append(("commit", [r["order_reference"] for r in args[3]]))
        return await original(*args, **kwargs)

    writer = AsyncMock(side_effect=record)
    monkeypatch.setattr(state, "record_finding_batch", writer)
    return run, refs, reader, writer, events


@pytest.mark.parametrize("mode", ["detect_only", "propose_actions"])
async def test_stages_source_then_commits_contiguous_findings_once(db, admin_user, monkeypatch, mode):
    actor, _ = admin_user
    run, refs, reader, writer, events = await setup(db, actor, monkeypatch, action_mode=mode)
    # A canceled order needs review; this also exercises Inc's propose-actions
    # configuration without creating a repair proposal.
    original = reader.side_effect

    async def canceled(*args, **kwargs):
        source = await original(*args, **kwargs)
        source["orders"][0]["state"] = "canceled"
        return source

    reader.side_effect = canceled
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    assert result["processed"] == len(refs)
    assert reader.await_count == len(refs)
    assert writer.await_count == 1
    assert events[: len(refs)] == [("read", ref) for ref in refs]
    assert events[-1] == ("commit", refs)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == len(refs)
    assert current.api_calls_used == 2 * len(refs)
    assert current.progress_json["pending_refs"] == []
    assert current.progress_json["source_staged_groups"] == 1
    assert len(await state.list_findings(db, actor.tenant_id, run.id)) == len(refs)
    assert not await state.list_proposals(db, actor.tenant_id, run_id=run.id)


async def test_flushes_prior_chunk_before_next_provider_read(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, _, writer, events = await setup(db, actor, monkeypatch, size=12)
    await run_investigation(db, actor.tenant_id, run.id)
    assert writer.await_count == 2
    assert events.index(("commit", refs[:10])) < events.index(("read", refs[10]))


async def test_cache_miss_flushes_completed_prefix_before_new_reservation(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, _, writer, events = await setup(db, actor, monkeypatch)
    base = staged_netsuite.StagedNetSuite

    class Miss(base):
        def __init__(self, *args, **kwargs):
            self.reserve = kwargs["reserve"]

        async def order(self, reference, **kwargs):
            if reference == refs[5]:
                assert await self.reserve(1)
                current = await state.get_run(db, actor.tenant_id, run.id)
                assert current.progress_json["processed"] == 5
                events.append(("new target read", reference))
            return await super().order(reference, **kwargs)

    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", Miss)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    assert writer.await_count == 2
    assert events.index(("commit", refs[:5])) < events.index(("new target read", refs[5]))


async def test_config_disable_discards_unpublished_prefix_without_skipping_it(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, _, _, _ = await setup(db, actor, monkeypatch)
    base = staged_netsuite.StagedNetSuite

    class Revoke(base):
        async def order(self, reference, **kwargs):
            if reference == refs[3]:
                config = await state.get_config(db, actor.tenant_id, run.config_id)
                config.enabled = False
                await db.commit()
            return await super().order(reference, **kwargs)

    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", Revoke)
    tenant, run_id = actor.tenant_id, run.id
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "stall"
    assert result["processed"] == 0
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    assert not await state.list_findings(db, tenant, run_id)


async def test_order_budget_keeps_a_contiguous_unprocessed_tail(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, reader, _, _ = await setup(db, actor, monkeypatch, max_orders=4)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "budget"
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == current.progress_json["processed"] == 4
    assert current.progress_json["pending_refs"] == refs[4:]
    assert reader.await_count == 4


async def test_source_failure_preserves_paid_snapshots_without_skipping_orders(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, reader, writer, _ = await setup(db, actor, monkeypatch)
    original = reader.side_effect

    async def fail(*args, **kwargs):
        if args[3] == refs[3]:
            raise RuntimeError("injected provider failure")
        return await original(*args, **kwargs)

    reader.side_effect = fail
    tenant, run_id = actor.tenant_id, run.id
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "error"
    writer.assert_not_awaited()
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    assert current.progress_json["processed"] == 0
    assert (
        await db.scalar(
            select(func.count())
            .select_from(TransactionSourceSnapshot)
            .where(
                TransactionSourceSnapshot.tenant_id == tenant,
            )
        )
        == 3
    )


async def test_failed_final_chunk_rolls_back_results_and_rewinds_memory_cursor(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, _, _, _ = await setup(db, actor, monkeypatch)
    original = finding_batch.persist

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("fail after lifecycle writes")

    monkeypatch.setattr(finding_batch, "persist", fail)
    tenant, run_id = actor.tenant_id, run.id
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "error"
    assert result["processed"] == 0
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    for model in (TransactionFinding, TransactionCaseObservation):
        assert await db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant)) == 0
