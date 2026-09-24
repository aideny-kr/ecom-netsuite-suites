from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import refund_reader
from app.services.transaction_ops.runner import run_investigation
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_transaction_ops_runner import NOW, REF, State, missing_target, source_order

REFS = [REF, "R100000002", "R100000003"]


@pytest.mark.parametrize("mode", ["success", "bad_batch", "fallback_budget", "cache_revoked", "actions"])
async def test_window_batch_consumption_preserves_budget_cursor_and_failure_guards(monkeypatch, mode):
    state = State(window=True, budget=200)
    mapping = state.run.config_snapshot["mapping_json"]
    mapping["solidus_refund_step_id"] = str(uuid4())
    if mode == "actions":
        mapping["action_mode"] = "propose_actions"
    state.run.progress_json = {
        "pending_refs": REFS.copy(),
        "scan_complete": True,
        "refund_scan_complete": True,
        "dependency_scan_complete": True,
    }
    cache = {}

    def evidence(ref):
        return {
            "order_reference": ref,
            "complete": True,
            "currency": "USD",
            "amount": "0",
            "observed_at": NOW.isoformat(),
        }

    async def batch_read(db, tenant, step, refs):
        assert state.events[-1] == ("reserve", 2, 0)
        if mode in {"bad_batch", "fallback_budget"}:
            if mode == "fallback_budget":
                state.budget = 0
            raise SourceReadError("refund_batch_identity_unproven")
        cache.update({ref: evidence(ref) for ref in refs[1:]})
        return evidence(refs[0])

    async def cache_get(db, tenant, step, ref, **kwargs):
        if ref in cache and mode == "cache_revoked":
            raise SourceReadError("source_not_found")
        return cache.pop(ref, None)

    batch = AsyncMock()
    batch.read.side_effect = batch_read
    batch.get.side_effect = cache_get
    monkeypatch.setattr(refund_reader, "RefundBatch", lambda: batch)
    single = AsyncMock(side_effect=lambda db, tenant, step, ref: evidence(ref))
    monkeypatch.setattr(refund_reader, "read_solidus_refunds", single)

    async def read_source(db, tenant, step, ref, **kwargs):
        result = deepcopy(source_order())
        result["orders"][0]["number"] = ref
        return result

    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _enabled=AsyncMock(return_value=True),
        _source_reader=read_source,
        _target_reader=AsyncMock(return_value=missing_target()),
        _order_mirror=AsyncMock(),
    )
    if mode == "fallback_budget":
        assert result["termination_reason"] == "budget"
        single.assert_not_awaited()
        assert state.run.progress_json["pending_refs"] == REFS
        assert state.events[-1] == ("reserve", 2, 0)
        return
    assert result["termination_reason"] == "done"
    assert state.run.progress_json["processed"] == 3
    assert not state.run.progress_json["pending_refs"]
    if mode == "success":
        batch.read.assert_awaited_once()
        single.assert_not_awaited()
        assert state.run.progress_json["source_refund_batch_hits"] == 2
    elif mode == "bad_batch":
        batch.read.assert_awaited_once()  # Failed batch is disabled for this run.
        assert single.await_count == 3
        assert state.run.progress_json["source_refund_batch_failures"] == 1
    elif mode == "cache_revoked":
        single.assert_not_awaited()
        for ref in REFS[1:]:
            assert state.reports[ref]["refund_evidence"]["source"]["complete"] is False
    else:
        batch.read.assert_not_awaited()
        batch.get.assert_not_awaited()
        assert single.await_count == 3
