"""Real committed keyset pages prove bounded batching and durable pending work."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionFinding
from app.services.transaction_ops import continuation, metabase_reader, staged_netsuite
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.normalization import _time
from app.services.transaction_ops.runner import ScanChangedError, _append_replica_page_progress, run_investigation
from tests import test_scope_first_runner as scope_tests

committed = scope_tests.committed
mixed = scope_tests.mixed


async def paged(committed, monkeypatch, *, size=200, max_orders=200, owned=True, empty_final=False):
    db, _, _ = committed
    run, refs, reader, writer, events, batch, own = await mixed(
        committed, monkeypatch, size=size, max_orders=max_orders
    )
    # The earlier helper's two-digit suffix only supports <100 references.
    # Preserve its captured list/set while generating valid nine-digit numbers.
    refs[:] = [f"R{100000000 + i:09d}" for i in range(size)]
    own.clear()
    own.update(refs[::20])
    version = (_time(run.params_json["window_start"]) + timedelta(hours=1)).isoformat()
    run.progress_json = {
        **run.progress_json,
        "pending_refs": refs[:20],
        "unscoped_replica_refs": refs[:20],
        "pending_source_versions": dict.fromkeys(refs[:20], version),
        "scan_complete": False,
        "scan_count": 20,
        "last_source_id": 20,
    }
    await db.commit()
    if not owned:
        own = set(refs)
        batch.side_effect = lambda *args, **kwargs: dict.fromkeys(args[3], "au")
        original = reader.side_effect

        async def all_owned(*args, **kwargs):
            body = await original(*args, **kwargs)
            body["orders"][0]["business_entity"] = {"id": "au"}
            return body

        reader.side_effect = all_owned

    original_detail = reader.side_effect

    async def versioned(*args, **kwargs):
        body = await original_detail(*args, **kwargs)
        body["orders"][0]["updated_at"] = version
        return body

    reader.side_effect = versioned

    async def page(*args, after_id, **kwargs):
        events.append(("page", after_id))
        indexes = range(after_id, min(after_id + 20, size))
        orders = [{"id": i + 1, "number": refs[i], "business_entity": None, "updated_at": version} for i in indexes]
        complete = after_id + len(orders) >= size and (not empty_final or not orders)
        return {
            "page_complete": True,
            "scan_complete": complete,
            "next_after_id": None if complete else orders[-1]["id"],
            "orders": orders,
        }

    pages = AsyncMock(side_effect=page)
    monkeypatch.setattr(metabase_reader, "read_order_page", pages)
    return run, refs, reader, writer, events, batch, own, pages


@pytest.mark.parametrize("empty_final", [False, True])
async def test_sparse_owned_pages_fill_one_financial_batch_without_foreign_reads(committed, monkeypatch, empty_final):
    db, actor, _ = committed
    run, refs, reader, writer, events, batch, own, pages = await paged(
        committed, monkeypatch, size=180 if empty_final else 200, empty_final=empty_final
    )
    snapshots = []
    original = state.update_progress

    async def checkpoint(*args, **kwargs):
        snapshots.append(deepcopy(args[3].progress_json))
        return await original(*args, **kwargs)

    monkeypatch.setattr(state, "update_progress", checkpoint)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == len(own)
    assert reader.await_count == len(own) and writer.await_count == 1
    assert [set(v) for k, v in events if k == "target_prefetch"] == [own]
    assert {ref for k, ref in events if k == "compare"} == own
    assert all(len(call.args[3]) <= 10 for call in batch.call_args_list)
    assert batch.await_count <= 2 * (len(refs) // 20) + 1
    assert max(len(p.get("pending_refs", [])) for p in snapshots) <= 29
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.orders_used == len(refs) and current.api_calls_held == 0
    assert current.progress_json["scan_count"] == len(refs)
    assert current.progress_json["outside_scope"] == len(refs) - len(own)
    assert current.progress_json["pending_refs"] == []
    assert current.progress_json["source_owned_fill_pages"] >= len(refs) // 20 - 2
    if empty_final:
        assert pages.call_args_list[-1].kwargs["after_id"] == len(refs)
        assert any(p["scan_complete"] and p["pending_refs"] for p in snapshots)


async def test_budget_continuations_preserve_buffered_owned_work_and_financial_uniqueness(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, _, events, _, own, _ = await paged(committed, monkeypatch, max_orders=100)
    runs = []
    for _ in range(4):
        result = await run_investigation(db, actor.tenant_id, run.id)
        current = await state.get_run(db, actor.tenant_id, run.id)
        runs.append(current)
        if result["termination_reason"] == "done":
            break
        assert result["termination_reason"] == "budget"
        assert all(r not in {ref for k, ref in events if k == "compare"} for r in current.progress_json["pending_refs"])
        run = await continuation.continue_budget_run(db, actor.tenant_id, run.id)
        assert run is not None
    else:
        pytest.fail("bounded fixture failed to complete")
    compared = [ref for k, ref in events if k == "compare"]
    assert set(compared) == own and len(compared) == len(own)
    assert reader.await_count == len(own)
    assert sum(r.orders_used for r in runs) == len(refs)
    assert all(r.orders_used <= 100 and r.api_calls_held == 0 for r in runs)
    assert current.progress_json["scan_count"] == len(refs) and not current.progress_json["pending_refs"]


@pytest.mark.parametrize("fault", ["page", "checkpoint", "disabled", "invalid"])
async def test_append_failure_keeps_old_owned_references_without_financial_effect(committed, monkeypatch, fault):
    db, actor, factory = committed
    run, refs, reader, writer, events, _, _, pages = await paged(committed, monkeypatch)
    tenant, run_id = actor.tenant_id, run.id
    original_page = pages.side_effect

    async def page(*args, **kwargs):
        if fault == "page":
            raise ValueError("injected_append_failure")
        if fault == "disabled":
            async with factory() as other:
                config = await state.get_config(other, actor.tenant_id, run.config_id)
                config.enabled = False
                await other.commit()
        value = await original_page(*args, **kwargs)
        if fault == "invalid":
            value["page_complete"] = False
        return value

    pages.side_effect = page
    if fault == "checkpoint":
        original = state.update_progress

        async def fail_save(*args, **kwargs):
            if args[3].progress_json.get("source_owned_fill_pages"):
                raise ValueError("injected_append_checkpoint_failure")
            return await original(*args, **kwargs)

        monkeypatch.setattr(state, "update_progress", fail_save)
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] in {"error", "stall"}
    current = await state.get_run(db, tenant, run_id)
    assert refs[0] in current.progress_json["pending_refs"]
    assert current.progress_json["last_source_id"] == (40 if fault == "disabled" else 20)
    reader.assert_not_awaited()
    writer.assert_not_awaited()
    assert not any(k in {"target_prefetch", "compare"} for k, _ in events)


async def test_paginated_buffered_and_detail_only_financial_reports_are_identical(committed, monkeypatch):
    db, actor, _ = committed
    reports = []
    for fallback in (True, False):
        run, refs, reader, _, _, batch, own, _ = await paged(committed, monkeypatch, size=100)
        if fallback:
            batch.side_effect = lambda *args, **kwargs: {}
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
        assert result["termination_reason"] == "done" and result["processed"] == len(own)
        assert reader.await_count == (len(refs) if fallback else len(own))
        findings = (await db.scalars(select(TransactionFinding).where(TransactionFinding.run_id == run.id))).all()
        balances = {}
        for finding in findings:
            balance = deepcopy(finding.report_json["balance"])
            assert balance["status"] == "incomplete"  # Buffering cannot manufacture refund proof.
            assert balance["amounts"]["order_total"] == {"source": "100.00", "target": "100.00", "delta": "0.00"}
            balance.pop("source_observed_at", None)
            balance.pop("target_observed_at", None)
            balances[finding.order_reference] = balance
        assert set(balances) == own
        reports.append(balances)
    assert reports[0] == reports[1]


async def test_cancelled_append_preserves_owned_candidate_and_old_cursor(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, writer, events, _, _, pages = await paged(committed, monkeypatch)
    tenant, run_id = actor.tenant_id, run.id
    started = asyncio.Event()

    async def wait(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    pages.side_effect = wait
    task = asyncio.create_task(run_investigation(db, tenant, run_id))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db.rollback()
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == [refs[0]]
    assert current.progress_json["last_source_id"] == 20
    reader.assert_not_awaited()
    writer.assert_not_awaited()
    assert not any(k in {"target_prefetch", "compare"} for k, _ in events)


async def test_lost_lease_during_append_cannot_advance_cursor_or_compare(committed, monkeypatch):
    db, actor, factory = committed
    run, refs, reader, writer, events, _, _, pages = await paged(committed, monkeypatch)
    tenant, run_id = actor.tenant_id, run.id
    original_page = pages.side_effect

    async def replaced(*args, **kwargs):
        async with factory() as other:
            current = await state.get_run(other, tenant, run_id, lock=True)
            current.lease_token = uuid4()
            await other.commit()
        return await original_page(*args, **kwargs)

    pages.side_effect = replaced
    result = await run_investigation(db, tenant, run_id)
    assert result["status"] == "yielded" and result["termination_reason"] == "stall"
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["pending_refs"] == [refs[0]]
    assert current.progress_json["last_source_id"] == 20
    reader.assert_not_awaited()
    writer.assert_not_awaited()
    assert not any(k in {"target_prefetch", "compare"} for k, _ in events)


async def test_mostly_owned_feed_keeps_overlap_and_processes_current_page_before_more_discovery(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, _, events, _, _, _ = await paged(committed, monkeypatch, size=40, owned=False)
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 40
    assert reader.await_count == 40
    kinds = [k for k, _ in events]
    assert kinds.index("target_prefetch") < kinds.index("page")
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.progress_json.get("source_owned_fill_pages", 0) == 0


async def test_full_detail_rechecks_scope_after_buffering_headers(committed, monkeypatch):
    db, actor, _ = committed
    run, refs, reader, _, events, _, own, _ = await paged(committed, monkeypatch)
    original = reader.side_effect
    changed = refs[0]

    async def moved(*args, **kwargs):
        body = await original(*args, **kwargs)
        if args[3] == changed:
            body["orders"][0]["business_entity"] = {"id": "other"}
        return body

    reader.side_effect = moved
    result = await run_investigation(db, actor.tenant_id, run.id)
    assert result["termination_reason"] == "done" and result["processed"] == 9
    assert {ref for k, values in events if k == "target_prefetch" for ref in values} == own - {changed}
    findings = (await db.scalars(select(TransactionFinding).where(TransactionFinding.run_id == run.id))).all()
    assert {f.order_reference for f in findings} == own - {changed}


@pytest.mark.parametrize("fault", ["id", "version", "cursor", "duplicate", "metadata"])
def test_invalid_appended_page_is_atomic_and_keeps_existing_versions(fault):
    params = {"window_start": "2026-09-29T00:00:00Z", "window_end": "2026-09-30T00:00:00Z"}
    config = {"subsidiary_id": "4", "mapping_json": {"business_entity_subsidiaries": {"au": "4"}}}
    progress = {
        "pending_refs": ["R100000001"],
        "last_source_id": 20,
        "scan_count": 20,
        "unscoped_replica_refs": ["R100000001"],
        "pending_source_versions": {"R100000001": "2026-09-29T01:00:00Z"},
    }
    before = deepcopy(progress)
    page = {
        "page_complete": True,
        "scan_complete": False,
        "next_after_id": 21,
        "orders": [{"id": 21, "number": "R100000021", "updated_at": "2026-09-29T02:00:00Z", "business_entity": None}],
    }
    if fault == "id":
        page["orders"][0]["id"] = 20
    elif fault == "version":
        page["orders"][0]["updated_at"] = "2026-09-28T02:00:00Z"
    elif fault == "cursor":
        page["next_after_id"] = 22
    elif fault == "duplicate":
        page["orders"][0]["number"] = progress["pending_refs"][0]
    else:
        page["page_complete"] = False
    with pytest.raises(ScanChangedError):
        _append_replica_page_progress(page, progress, params, config)
    assert progress == before
