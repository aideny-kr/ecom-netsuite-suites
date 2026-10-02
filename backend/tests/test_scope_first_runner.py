"""Committed mixed-entity runs prove call reduction, reports and cursor durability."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionFinding
from app.services.transaction_ops import continuation, source_scope, staged_netsuite
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.runner import run_investigation
from tests import test_concurrent_source_pipeline as pipeline_tests
from tests.test_metabase_replica_reader import BINDING

committed = pipeline_tests.committed


async def mixed(committed, monkeypatch, *, size=100, unknown=False, max_orders=100):
    db, actor, _ = committed
    run, refs, reader, writer, events, _ = await pipeline_tests.parallel_setup(
        committed,
        monkeypatch,
        size=size,
        max_orders=max_orders,
        mapping={"metabase_replica": BINDING, "business_entity_subsidiaries": {"au": "1"}},
    )
    run.progress_json = {**run.progress_json, "unscoped_replica_refs": refs}
    await db.commit()
    own = set(refs[::20])
    original = reader.side_effect

    async def read(*args, **kwargs):
        body = await original(*args, **kwargs)
        body["orders"][0]["business_entity"] = {"id": "au" if args[3] in own else "other"}
        return body

    async def scopes(*args, **kwargs):
        events.append(("scope", list(args[3])))
        return {} if unknown else {ref: "au" if ref in own else "other" for ref in args[3]}

    reader.side_effect = read
    batch = AsyncMock(side_effect=scopes)
    monkeypatch.setattr(source_scope, "read_order_scopes", batch)
    return run, refs, reader, writer, events, batch, own


@pytest.mark.parametrize("fallback", [False, True])
async def test_ninety_five_percent_foreign_same_financial_results_without_foreign_targets(
    committed, monkeypatch, fallback
):
    db, actor, _ = committed
    run, refs, reader, writer, events, batch, own = await mixed(committed, monkeypatch, unknown=fallback)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == len(own)
    findings = (await db.scalars(select(TransactionFinding).where(TransactionFinding.run_id == run.id))).all()
    assert {f.order_reference for f in findings} == own
    assert all(f.report_json["balance"]["status"] != "matched" for f in findings)
    assert {ref for kind, ref in events if kind == "compare"} == own
    targets = {ref for kind, values in events if kind == "target_prefetch" for ref in values}
    assert targets <= own
    assert reader.await_count == (len(refs) if fallback else len(own))
    assert all(1 <= len(call.args[3]) <= 10 for call in batch.call_args_list)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == len(refs)
    assert current.progress_json["pending_refs"] == []
    assert current.progress_json["outside_scope"] == len(refs) - len(own)
    assert current.progress_json.get("source_scope_rejected", 0) == (0 if fallback else 95)
    assert current.api_calls_held == 0


async def test_all_foreign_is_one_scope_read_and_one_cursor_checkpoint(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, writer, events, batch, _ = await mixed(committed, monkeypatch, size=10)
    batch.side_effect = None
    batch.return_value = dict.fromkeys(refs, "other")
    snapshots = []
    original_checkpoint = state.update_progress

    async def record_checkpoint(*args, **kwargs):
        snapshots.append(deepcopy(args[3].progress_json))
        return await original_checkpoint(*args, **kwargs)

    checkpoint = AsyncMock(side_effect=record_checkpoint)
    monkeypatch.setattr(state, "update_progress", checkpoint)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 0
    reader.assert_not_awaited()
    writer.assert_not_awaited()
    batch.assert_awaited_once()
    assert not any(kind in {"compare", "target_prefetch"} for kind, _ in events)
    assert sum(item.get("source_scope_rejected", 0) == 10 for item in snapshots) == 1
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == 10 and current.api_calls_used == 2
    assert current.progress_json["pending_refs"] == []


@pytest.mark.parametrize("fault", ["provider", "checkpoint", "disabled"])
async def test_failure_cannot_lose_uncheckpointed_candidates_or_create_findings(committed, monkeypatch, fault):
    db, actor, factory = committed
    run, refs, reader, writer, events, batch, _ = await mixed(committed, monkeypatch, size=10)
    initial = deepcopy(run.progress_json)
    tenant, run_id = actor.tenant_id, run.id

    async def fail(*args, **kwargs):
        if fault == "provider":
            raise ValueError("injected_header_failure")
        if fault == "disabled":
            async with factory() as change:
                config = await state.get_config(change, actor.tenant_id, run.config_id)
                config.enabled = False
                await change.commit()
        return dict.fromkeys(refs, "other")

    batch.side_effect = fail
    if fault == "checkpoint":
        original = state.update_progress

        async def checkpoint(*args, **kwargs):
            if args[3].progress_json.get("source_scope_rejected"):
                raise ValueError("injected_checkpoint_failure")
            return await original(*args, **kwargs)

        monkeypatch.setattr(state, "update_progress", checkpoint)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] in {"error", "stall"}
    reader.assert_not_awaited()
    writer.assert_not_awaited()
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == initial["pending_refs"]
    assert current.progress_json.get("outside_scope", 0) == 0


async def test_optimized_and_full_detail_baseline_have_identical_financial_reports(committed, monkeypatch):
    db, actor, _ = committed
    reports = []
    for fallback in (True, False):
        run, _, _, _, _, _, own = await mixed(committed, monkeypatch, unknown=fallback)
        base = staged_netsuite.StagedNetSuite

        class Matched(base):
            async def order(self, reference, **kwargs):
                target = await super().order(reference, **kwargs)
                target["lookup"] = {"complete": True, "count": 1}
                target["orders"] = [
                    {
                        "order_reference": reference,
                        "record_id": "77",
                        "header_complete": True,
                        "header": {
                            "id": "77",
                            "subsidiary": {"id": "1"},
                            "currency": {"id": "1"},
                            "total": "100.00",
                            "taxTotal": "0.00",
                        },
                        "currency_metadata": {"id": "1", "symbol": "USD", "currencyPrecision": 2},
                    }
                ]
                return target

            async def refund(self, reference, *args, **kwargs):
                return {"complete": True, "order_reference": reference, "currency": "USD", "amount": "0.00"}

        monkeypatch.setattr(staged_netsuite, "StagedNetSuite", Matched)

        result = await run_investigation(db, actor.tenant_id, run.id)
        assert result["termination_reason"] == "done"
        findings = (await db.scalars(select(TransactionFinding).where(TransactionFinding.run_id == run.id))).all()
        balances = {}
        for finding in findings:
            balance = deepcopy(finding.report_json["balance"])
            assert balance["status"] == "incomplete"  # Missing refund proof remains unknown.
            assert balance["amounts"]["order_total"] == {"source": "100.00", "target": "100.00", "delta": "0.00"}
            balance.pop("source_observed_at", None)
            balance.pop("target_observed_at", None)
            balances[finding.order_reference] = balance
        assert set(balances) == own
        reports.append(balances)
    assert reports[0] == reports[1]


async def test_budget_continuation_rejects_only_remaining_candidates_after_durable_checkpoint(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, writer, _, batch, _ = await mixed(committed, monkeypatch, size=20, max_orders=10)
    batch.side_effect = lambda *args, **kwargs: dict.fromkeys(args[3], "other")
    first = await run_investigation(db, actor.tenant_id, run.id)
    assert first["termination_reason"] == "budget"
    parent = await state.get_run(db, actor.tenant_id, run.id)
    assert parent.progress_json["pending_refs"] == refs[10:]
    assert parent.progress_json["outside_scope"] == 10
    child = await continuation.continue_budget_run(db, actor.tenant_id, run.id)
    assert child is not None
    assert (await run_investigation(db, actor.tenant_id, child.id))["termination_reason"] == "done"
    current = await state.get_run(db, actor.tenant_id, child.id)
    assert current.progress_json["pending_refs"] == [] and current.progress_json["outside_scope"] == 20
    assert [list(call.args[3]) for call in batch.call_args_list] == [refs[:10], refs[10:]]
    assert parent.orders_used == child.orders_used == 10
    reader.assert_not_awaited()
    writer.assert_not_awaited()


async def test_cancelled_scope_read_retains_cursor_and_does_not_prepare_targets(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, writer, events, batch, _ = await mixed(committed, monkeypatch, size=10)
    tenant, run_id = actor.tenant_id, run.id
    started, release = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await release.wait()
        return dict.fromkeys(refs, "other")

    batch.side_effect = pending
    task = asyncio.create_task(run_investigation(db, tenant, run_id))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db.rollback()
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    assert current.api_calls_used == 2 and current.orders_used == 0
    reader.assert_not_awaited()
    writer.assert_not_awaited()
    assert not any(kind == "target_prefetch" for kind, _ in events)


async def test_legacy_owning_inc_profile_keeps_provider_overlap_and_has_no_header_overhead(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, _, events, target = await pipeline_tests.parallel_setup(
        committed, monkeypatch, mapping={"metabase_replica": BINDING}
    )
    run.progress_json = {**run.progress_json, "unscoped_replica_refs": refs}
    await db.commit()
    original = reader.side_effect

    async def source(*args, **kwargs):
        assert target.is_set(), "Legacy source and NetSuite must retain overlap"
        return await original(*args, **kwargs)

    reader.side_effect = source
    header = AsyncMock(return_value={})
    monkeypatch.setattr(source_scope, "read_order_scopes", header)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 10
    header.assert_not_awaited()
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == 20
    assert not current.progress_json.get("source_scope_batches")
    assert ("target_prefetch", [refs[0]]) in events


@pytest.mark.parametrize("code", ["source_transport_failed", "source_http_error", "invalid_source_response"])
async def test_optional_list_failure_falls_back_once_without_stopping_daily_work(committed, monkeypatch, code):
    from app.services.transaction_ops.source_reader import SourceReadError

    db, actor, _ = committed
    run, refs, reader, _, events, batch, own = await mixed(committed, monkeypatch)
    batch.side_effect = SourceReadError(code)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == len(own)
    batch.assert_awaited_once()
    assert reader.await_count == len(refs)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.progress_json["source_scope_read_fallbacks"] == 1
    assert current.progress_json.get("read_retry_count", 0) == 0
    assert not current.progress_json.get("last_read_failure")
    assert {ref for kind, values in events if kind == "target_prefetch" for ref in values} <= own


async def test_scope_authentication_failure_cannot_be_downgraded_to_optimization_fallback(committed, monkeypatch):
    from app.services.transaction_ops.source_reader import SourceReadError

    db, actor, _ = committed
    run, refs, reader, writer, _, batch, _ = await mixed(committed, monkeypatch, size=10)
    tenant, run_id = actor.tenant_id, run.id
    batch.side_effect = SourceReadError("source_authentication_failed")
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "error"
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == refs
    assert current.progress_json["last_read_failure"]["stage"] == "source_scope_batch"
    assert not current.progress_json.get("source_scope_read_fallbacks")
    reader.assert_not_awaited()
    writer.assert_not_awaited()


@pytest.mark.parametrize("prior_owned", [0, 20])
async def test_explicit_inc_profile_uses_scope_mix_without_losing_overlap(committed, monkeypatch, prior_owned):
    db, actor, _ = committed
    run, refs, reader, _, events, target = await pipeline_tests.parallel_setup(
        committed,
        monkeypatch,
        size=20,
        mapping={"metabase_replica": BINDING, "business_entity_subsidiaries": {"inc": "1"}},
    )
    run.progress_json = {**run.progress_json, "unscoped_replica_refs": refs, "processed": prior_owned}
    await db.commit()
    original = reader.side_effect

    async def source(*args, **kwargs):
        assert target.is_set(), "Mostly in-scope profiles retain provider overlap"
        body = await original(*args, **kwargs)
        body["orders"][0]["business_entity"] = {"id": "inc"}
        return body

    reader.side_effect = source
    header = AsyncMock(side_effect=lambda *args, **kwargs: dict.fromkeys(args[3], "inc"))
    monkeypatch.setattr(source_scope, "read_order_scopes", header)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == prior_owned + 20
    assert header.await_count == (0 if prior_owned else 1)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == 40 + (0 if prior_owned else 2)
