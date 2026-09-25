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


async def setup(
    db, actor, monkeypatch, *, max_orders=100, size=10, action_mode="detect_only", phase="orders", mapping=None
):
    if mapping and mapping.get("metabase_replica"):
        from app.services.transaction_ops import metabase_reader

        monkeypatch.setattr(metabase_reader, "_connector", AsyncMock())
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
            **(mapping or {}),
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
        pending_refs=refs, scan_complete=True, refund_scan_complete=True, destination_scan_complete=True, phase=phase
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
@pytest.mark.parametrize("phase", ["orders", "refunds", "destination"])
async def test_stages_source_then_commits_contiguous_findings_once(db, admin_user, monkeypatch, mode, phase):
    actor, _ = admin_user
    run, refs, reader, writer, events = await setup(db, actor, monkeypatch, action_mode=mode, phase=phase)
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
    assert current.progress_json["source_staged_hits"] == len(refs)
    assert current.progress_json.get("source_snapshot_hits", 0) == 0
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


@pytest.mark.parametrize("phase", ["refunds", "destination"])
async def test_later_phase_fetches_fresh_source_even_with_same_cycle_snapshots(db, admin_user, monkeypatch, phase):
    from uuid import UUID

    from app.services.transaction_ops import source_snapshot

    actor, _ = admin_user
    run, refs, reader, _, events = await setup(db, actor, monkeypatch, phase=phase)
    connection_id = UUID(run.config_snapshot["source_connection_id"])
    for ref in refs:
        source = await reader(db, actor.tenant_id, None, ref, source_connection_id=connection_id)
        assert await source_snapshot.save(
            db, actor.tenant_id, connection_id, ref, source, now=datetime.now(timezone.utc)
        )
    reader.reset_mock()
    events.clear()
    assert (await run_investigation(db, actor.tenant_id, run.id))["termination_reason"] == "done"
    assert reader.await_count == len(refs)


@pytest.mark.parametrize("phase", ["orders", "refunds"])
async def test_out_of_scope_reference_flushes_prefix_before_skipping(db, admin_user, monkeypatch, phase):
    from tests.test_metabase_replica_reader import BINDING

    actor, _ = admin_user
    run, refs, reader, writer, events = await setup(
        db, actor, monkeypatch, phase=phase, mapping={"metabase_replica": BINDING} if phase == "orders" else None
    )
    if phase == "orders":
        run.progress_json = {**run.progress_json, "unscoped_replica_refs": refs}
        await db.commit()
    original = reader.side_effect

    async def other_entity(*args, **kwargs):
        source = await original(*args, **kwargs)
        if args[3] == refs[3]:
            source["orders"][0]["business_entity"] = {"id": "different", "name": "Other entity"}
        return source

    reader.side_effect = other_entity
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.progress_json["processed"] == 9
    assert current.progress_json["outside_scope"] == 1
    assert current.progress_json["pending_refs"] == []
    assert writer.await_count == 2
    assert ("commit", refs[:3]) in events


async def test_source_scope_failure_mid_chunk_preserves_completed_prefix(db, admin_user, monkeypatch):
    actor, _ = admin_user
    run, refs, reader, _, _ = await setup(db, actor, monkeypatch)
    original = reader.side_effect

    async def invalid(*args, **kwargs):
        source = await original(*args, **kwargs)
        if args[3] == refs[3]:
            source["orders"][0]["business_entity"] = {"id": "other", "name": "Other"}
        return source

    reader.side_effect = invalid
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "stall"
    assert result["processed"] == 3
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.progress_json["pending_refs"] == refs[3:]
    assert current.progress_json["reason"] == "source_subsidiary_unproven"


@pytest.mark.parametrize("expired", ["lease", "deadline"])
async def test_buffer_expiry_rolls_back_without_leaving_deadline_run_open(db, admin_user, monkeypatch, expired):
    actor, _ = admin_user
    run, refs, _, _, _ = await setup(db, actor, monkeypatch)
    offset = [timedelta(0)]
    base = staged_netsuite.StagedNetSuite

    class Expire(base):
        async def order(self, reference, **kwargs):
            if reference == refs[3]:
                offset[0] += timedelta(minutes=16 if expired == "deadline" else 4)
            return await super().order(reference, **kwargs)

    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", Expire)
    tenant, run_id = actor.tenant_id, run.id
    result = await run_investigation(db, tenant, run_id, _clock=lambda: datetime.now(timezone.utc) + offset[0])
    assert result["status"] == ("finished" if expired == "deadline" else "yielded")
    assert result["termination_reason"] == ("budget" if expired == "deadline" else "stall")
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    assert current.progress_json["processed"] == 0
    if expired == "deadline":
        assert current.status == "finished" and current.termination_reason == "budget"
    assert not await state.list_findings(db, tenant, run_id)


async def test_unstageable_snapshot_uses_paid_single_read_fallback(db, admin_user, monkeypatch):
    from app.services.transaction_ops import source_snapshot

    actor, _ = admin_user
    run, refs, reader, _, _ = await setup(db, actor, monkeypatch, size=2)
    # Failed projection/credential partition cannot become reusable evidence.
    # The bounded fallback pays for a fresh read instead of trusting that body.
    monkeypatch.setattr(source_snapshot, "save", AsyncMock(return_value=False))
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    assert reader.await_count == 2 * len(refs)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == len(refs)
    assert current.api_calls_used == 4 * len(refs)


@pytest.mark.parametrize("replaced", [False, True])
async def test_fresh_staging_keeps_proposal_preflight_but_replaced_snapshot_does_not(
    db, admin_user, monkeypatch, replaced
):
    from unittest.mock import Mock

    from app.services.transaction_ops import netsuite_create, source_snapshot

    actor, _ = admin_user
    run, refs, _, _, _ = await setup(db, actor, monkeypatch, size=2, action_mode="propose_actions")
    prepare = Mock(side_effect=netsuite_create.CreateInputError("test_preflight_blocked"))
    monkeypatch.setattr(netsuite_create, "prepare_create_input", prepare)
    original = source_snapshot.load

    async def replace(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result and replaced:
            # The authenticated loader returned a later replacement, not the
            # exact body validated by this invocation's prefetch.
            result["read_at"] = datetime.now(timezone.utc).isoformat()
        return result

    monkeypatch.setattr(source_snapshot, "load", replace)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    reports = [row.report_json for row in await state.list_findings(db, actor.tenant_id, run.id)]
    assert all(r["comparison"]["recommended_action"] == "propose_missing_sync" for r in reports)
    assert prepare.call_count == (0 if replaced else len(refs))
    assert not await state.list_proposals(db, actor.tenant_id, run_id=run.id)


async def test_scope_error_plus_revocation_does_not_escape_exception_cleanup(db, admin_user, monkeypatch):
    from app.services.transaction_ops import source_snapshot

    actor, _ = admin_user
    run, refs, reader, _, _ = await setup(db, actor, monkeypatch)
    original_read, original_load = reader.side_effect, source_snapshot.load

    async def other(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if args[3] == refs[3]:
            result["orders"][0]["business_entity"] = {"id": "other", "name": "Other"}
        return result

    async def disable(*args, **kwargs):
        result = await original_load(*args, **kwargs)
        if result and args[3] == refs[3]:
            config = await state.get_config(db, actor.tenant_id, run.config_id)
            config.enabled = False
            await db.commit()
        return result

    reader.side_effect = other
    monkeypatch.setattr(source_snapshot, "load", disable)
    tenant, run_id = actor.tenant_id, run.id
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "stall"
    assert result["processed"] == 0
    assert (await state.get_run(db, tenant, run_id)).progress_json["pending_refs"] == refs


@pytest.mark.parametrize("cached", [False, True])
async def test_fresh_staging_accepts_framework_millisecond_detail_for_microsecond_page(
    db, admin_user, monkeypatch, cached
):
    from uuid import UUID

    from app.services.transaction_ops import source_snapshot

    actor, _ = admin_user
    run, refs, reader, writer, _ = await setup(db, actor, monkeypatch, size=3)
    detail_version = datetime.now(timezone.utc).replace(microsecond=458000) - timedelta(days=1)
    page_version = detail_version + timedelta(microseconds=810)
    run.progress_json = {
        **run.progress_json,
        "pending_source_versions": dict.fromkeys(refs, page_version.isoformat()),
    }
    await db.commit()
    original = reader.side_effect

    async def detail(*args, **kwargs):
        value = await original(*args, **kwargs)
        value["orders"][0]["updated_at"] = detail_version.isoformat(timespec="milliseconds")
        return value

    reader.side_effect = detail
    if cached:
        connection = UUID(run.config_snapshot["source_connection_id"])
        for ref in refs:
            value = await detail(db, actor.tenant_id, None, ref)
            assert await source_snapshot.save(
                db, actor.tenant_id, connection, ref, value, now=datetime.now(timezone.utc)
            )

    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    # Existing rounded snapshots still need a paid fresh read. The exact fresh
    # observation is then consumed once, without another provider call.
    assert reader.await_count == len(refs)
    assert writer.await_count == 1
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.progress_json["source_staged_hits"] == len(refs)
    assert current.progress_json.get("source_snapshot_hits", 0) == 0
    assert current.api_calls_used == 2 * len(refs)
    rows = (
        await db.scalars(
            select(TransactionSourceSnapshot).where(TransactionSourceSnapshot.tenant_id == actor.tenant_id)
        )
    ).all()
    assert all(row.source_updated_at == detail_version for row in rows)
    assert all(
        row.evidence_json["evidence"]["orders"][0]["updated_at"] == detail_version.isoformat(timespec="milliseconds")
        for row in rows
    )


@pytest.mark.parametrize("reason", ["next_millisecond", "submillisecond_detail", "replaced_snapshot"])
async def test_fresh_staging_precision_exception_cannot_admit_other_versions(db, admin_user, monkeypatch, reason):
    from app.services.transaction_ops import source_snapshot

    actor, _ = admin_user
    run, refs, reader, _, _ = await setup(db, actor, monkeypatch, size=3)
    detail_version = datetime.now(timezone.utc).replace(microsecond=458000) - timedelta(days=1)
    page_version = detail_version + timedelta(microseconds=810)
    if reason == "next_millisecond":
        page_version = detail_version + timedelta(milliseconds=1)
    elif reason == "submillisecond_detail":
        detail_version += timedelta(microseconds=800)
    run.progress_json = {
        **run.progress_json,
        "pending_source_versions": dict.fromkeys(refs, page_version.isoformat()),
    }
    await db.commit()
    original = reader.side_effect

    async def detail(*args, **kwargs):
        value = await original(*args, **kwargs)
        value["orders"][0]["updated_at"] = detail_version.isoformat()
        return value

    reader.side_effect = detail
    if reason == "replaced_snapshot":
        original_load = source_snapshot.load

        async def replaced(*args, **kwargs):
            value = await original_load(*args, **kwargs)
            if value:
                value["read_at"] = datetime.now(timezone.utc).isoformat()
            return value

        monkeypatch.setattr(source_snapshot, "load", replaced)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done"
    assert reader.await_count == 2 * len(refs)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.progress_json.get("source_staged_hits", 0) == 0
    assert current.api_calls_used == 4 * len(refs)
