from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import staged_netsuite as staged
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
REFS = ["R123456789", "R987654321"]


def target(ref):
    return {"complete": True, "lookup": {"count": 1}, "orders": [], "observed_at": NOW.isoformat()}


@pytest.fixture
async def adapter(monkeypatch):
    saved = {}

    async def save(db, tenant, run, kind, context, config, data, **kw):
        identifier = str(uuid4())
        saved[identifier] = (kind, context, deepcopy(data[kind]))
        return identifier

    async def load(db, tenant, identifier, kind, context, config, **kw):
        record = saved.get(identifier)
        return deepcopy(record[2]) if record and record[:2] == (kind, context) else None

    monkeypatch.setattr(staged.store, "save", AsyncMock(side_effect=save))
    monkeypatch.setattr(staged.store, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(staged.bulk, "read_orders", AsyncMock(return_value={"orders": {r: target(r) for r in REFS}}))
    monkeypatch.setattr(
        staged.bulk,
        "read_refunds",
        AsyncMock(return_value={"refunds": {r: {"complete": True, "amount": "0"} for r in REFS}}),
    )

    async def read(stage, factory, **kw):
        return await factory()

    return staged.StagedNetSuite(
        None,
        uuid4(),
        SimpleNamespace(id=uuid4(), created_at=NOW, max_api_calls=2000, api_calls_used=0, api_calls_held=0),
        {"netsuite_connection_id": str(uuid4()), "netsuite_account_id": "6738075", "subsidiary_id": "1"},
        SimpleNamespace(reference_field="tranid", refund_adjustments=None),
        {"phase": "orders", "pending_refs": REFS.copy()},
        clock=lambda: NOW + timedelta(seconds=30),
        reserve=AsyncMock(return_value=True),
        read=AsyncMock(side_effect=read),
        checkpoint=AsyncMock(),
    )


async def test_resume_uses_durable_batches_with_zero_provider_calls(adapter):
    first = await adapter.order(REFS[0])
    refund = await adapter.refund(REFS[0], first)
    assert first["observed_at"] == NOW.isoformat() and refund["amount"] == "0"
    assert adapter.reserve.await_count == 2
    adapter.progress["pending_refs"] = REFS[1:]
    # A fresh process has no local cache, but preserves the checkpoint IDs.
    adapter.cache = {}
    adapter.attempted = set()
    adapter.order_id = None
    resumed = await adapter.order(REFS[1])
    assert await adapter.refund(REFS[1], resumed) == refund
    assert staged.bulk.read_orders.await_count == 1 and staged.bulk.read_refunds.await_count == 1
    assert adapter.reserve.await_count == 2


async def test_partial_batch_falls_back_without_persisting_or_releasing_unearned_budget(adapter):
    staged.bulk.read_orders.side_effect = NetSuiteEvidenceError("bulk_page_incomplete")
    assert await adapter.order(REFS[0]) is None
    staged.store.save.assert_not_awaited()
    assert adapter.reserve.call_args.kwargs == {"hold": True}
    assert adapter.read.call_args.kwargs == {"held": 35, "data_calls": 32}
    assert adapter.progress["native_orders_batch_failures"] == 1


async def test_insufficient_batch_budget_leaves_single_read_available(adapter):
    adapter.reserve.return_value = False
    assert await adapter.order(REFS[0]) is None
    adapter.read.assert_not_awaited()


async def test_phase_or_target_change_cannot_reuse_refund_snapshot(adapter):
    first = await adapter.order(REFS[0])
    await adapter.refund(REFS[0], first)
    assert await adapter.refund(REFS[0], {**first, "observed_at": (NOW + timedelta(seconds=1)).isoformat()}) is None
    adapter.progress["phase"] = "dependencies"
    await adapter.order(REFS[0])
    assert staged.bulk.read_orders.await_count == 2


async def test_runner_consumes_staged_evidence_and_keeps_final_checkpoint(monkeypatch):
    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_ops_runner import State, missing_target, source_order

    state = State()
    native = SimpleNamespace(order=AsyncMock(return_value=missing_target()), refund=AsyncMock())
    monkeypatch.setattr(staged, "StagedNetSuite", lambda *a, **kw: native)
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=AsyncMock(return_value=source_order()),
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: __import__("tests.test_transaction_ops_runner", fromlist=["NOW"]).NOW,
    )
    assert result["termination_reason"] == "done" and result["processed"] == 1
    native.order.assert_awaited_once()
    assert state.run.progress_json["pending_refs"] == []


async def test_batch_failure_does_not_retry_for_every_pending_reference(adapter):
    staged.bulk.read_orders.side_effect = NetSuiteEvidenceError("bulk_page_incomplete")
    assert await adapter.order(REFS[0]) is None
    adapter.progress["pending_refs"] = REFS[1:]
    assert await adapter.order(REFS[1]) is None
    assert staged.bulk.read_orders.await_count == 1


async def test_oversized_stage_falls_back_but_auth_storage_errors_still_stop(adapter):
    staged.store.save.side_effect = NetSuiteEvidenceError("batch_size_budget")
    assert await adapter.order(REFS[0]) is None
    adapter.progress["pending_refs"] = REFS[1:]
    assert await adapter.order(REFS[1]) is None
    assert adapter.reserve.await_count == 1 and adapter.checkpoint.await_count == 0
    adapter.cache.clear()
    adapter.attempted.clear()
    adapter.progress["pending_refs"] = REFS.copy()
    staged.store.save.side_effect = NetSuiteEvidenceError("batch_credentials_changed")
    with pytest.raises(NetSuiteEvidenceError, match="batch_credentials_changed"):
        await adapter.order(REFS[0])


@pytest.mark.parametrize("remaining", [37, 90])
async def test_small_budget_uses_individual_path_without_spending_on_batch(adapter, remaining):
    adapter.run.max_api_calls = remaining
    assert await adapter.order(REFS[0]) is None
    adapter.reserve.assert_not_awaited()
    staged.bulk.read_orders.assert_not_awaited()


async def test_source_updates_and_refund_observations_after_prefetch_require_fresh_target(adapter):
    first = await adapter.order(REFS[0], source_updated_at=NOW - timedelta(minutes=1))
    assert first
    assert await adapter.order(REFS[1], source_updated_at=NOW + timedelta(seconds=1)) is None
    refund = await adapter.refund(REFS[0], first)
    assert refund
    # The report carries its real timestamp; a newer source read invalidates it.
    refund["observed_at"] = NOW.isoformat()
    assert await adapter.refund(REFS[0], first, source_observed_at=NOW + timedelta(seconds=1)) is None


async def test_delayed_continuation_does_not_reuse_old_financial_observation(adapter, monkeypatch):
    await adapter.order(REFS[0])
    adapter.clock = lambda: NOW + timedelta(minutes=16)
    # Real storage filters the collection interval; model that boundary here.
    staged.store.load.return_value = None
    staged.store.load.side_effect = None
    adapter.cache = {}
    adapter.attempted = set()
    staged.bulk.read_orders.return_value["orders"][REFS[0]]["observed_at"] = adapter.clock().isoformat()
    result = await adapter.order(REFS[0])
    assert result["observed_at"] == adapter.clock().isoformat()
    assert staged.bulk.read_orders.await_count == 2
    assert staged.store.load.call_args.kwargs["since"] == NOW + timedelta(minutes=6)
