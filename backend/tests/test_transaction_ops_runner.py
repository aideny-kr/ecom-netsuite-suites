from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops.netsuite_refunds import MAX_REFUND_CALLS
from app.services.transaction_ops.runner import run_investigation

NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
REF = "R100000001"


def source_order():
    return {
        "source": "framework",
        "scope": "order",
        "page_complete": True,
        "read_at": NOW.isoformat(),
        "orders": [
            {
                "id": "1",
                "number": REF,
                "currency": "USD",
                "state": "complete",
                "requires_review": False,
                "business_entity": None,
                "updated_at": (NOW - timedelta(hours=1)).isoformat(),
                "total": "100",
                "item_total": "100",
                "ship_total": "0",
                "tax_total": "0",
                "included_tax_total": "0",
                "additional_tax_total": "0",
                "adjustment_total": "0",
                "adjustments": [],
                "line_items": [{"id": "11", "quantity": "1", "price": "100", "total": "100", "adjustments": []}],
                "shipments": [{"id": "1", "cost": "0", "adjustments": []}],
            }
        ],
    }


def missing_target():
    return {
        "provider": "netsuite",
        "observed_at": NOW.isoformat(),
        "complete": True,
        "orders": [],
        "lookup": {"complete": True},
        "scope": {"account_id": "6738075", "subsidiary_id": "1"},
        "api_calls": 1,
    }


class State:
    def __init__(self, budget=100, window=False):
        self.tenant, self.run_id, self.token = uuid4(), uuid4(), uuid4()
        params = (
            {"order_references": [REF]}
            if not window
            else {
                "order_references": [],
                "window_start": (NOW - timedelta(hours=2)).isoformat(),
                "window_end": NOW.isoformat(),
            }
        )
        self.run = SimpleNamespace(
            id=self.run_id,
            config_id=uuid4(),
            params_json=params,
            progress_json={},
            status="pending",
            termination_reason=None,
            deadline_at=NOW + timedelta(minutes=15),
            config_snapshot={
                "source_step_id": str(uuid4()),
                "netsuite_connection_id": str(uuid4()),
                "netsuite_account_id": "6738075",
                "subsidiary_id": "1",
                "record_type": "salesorder",
                "target_step_id": None,
                "mapping_json": {
                    "reference_field": "tranid",
                    "currency_minor_units": {"USD": 2},
                    "business_entity_subsidiaries": {"legacy": "1"},
                },
            },
        )
        self.budget, self.events, self.reports, self.claimable = budget, [], {}, True

    async def get_run(self, *args, **kwargs):
        return self.run

    async def get_config(self, *args, **kwargs):
        return SimpleNamespace(enabled=True)

    async def claim_run(self, *args, **kwargs):
        if not self.claimable:
            return None
        self.run.status = "running"
        self.run.lease_token = self.token
        return self.token

    async def reserve_budget(self, *args, api_calls=0, orders=0, hold=False, lease_token=None, **kwargs):
        assert lease_token == self.token
        self.events.append(("reserve", api_calls, orders))
        if api_calls > self.budget:
            self.run.status, self.run.termination_reason = "finished", "budget"
            return False
        self.budget -= api_calls
        if hold:
            self.held = getattr(self, "held", 0) + api_calls
        return True

    async def settle_budget(self, *args, release, spent, lease_token=None, **kwargs):
        assert lease_token == self.token
        # The real settle refuses to release more than is held; so does this one.
        assert 0 <= spent <= release <= getattr(self, "held", 0)
        self.held -= release
        self.events.append(("settle", release, spent))
        self.budget += release - spent
        return True

    async def update_progress(self, *args, lease_token=None, **kwargs):
        assert lease_token == self.token
        self.run.progress_json = deepcopy(args[3].progress_json)

    async def record_finding(self, *args, lease_token=None, **kwargs):
        assert lease_token == self.token
        self.reports[args[3]] = deepcopy(args[4])

    async def unseen_references(self, db, tenant_id, run_id, references, *, since=None):
        return [reference for reference in references if reference not in self.reports]

    async def finish_run(self, *args, lease_token=None, **kwargs):
        assert lease_token == self.token
        self.run.status, self.run.termination_reason = "finished", args[3]
        return self.run


async def execute(state, source=None, target=None, page=None, enabled=True):
    async def read_source(*args, **kwargs):
        state.events.append("source")
        return source or source_order()

    async def read_target(*args, **kwargs):
        state.events.append("target")
        return target or missing_target()

    return await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=read_source,
        _target_reader=read_target,
        _page_reader=page,
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=enabled),
        _clock=lambda: NOW,
    )


@pytest.mark.parametrize("budget, expected", [(100, "done"), (2, "budget")])
async def test_transient_source_retry_reserves_full_cost_and_keeps_order_cursor(budget, expected):
    from app.services.transaction_ops.source_reader import SourceReadError

    state = State(budget=budget)
    reader = AsyncMock(side_effect=[SourceReadError("source_transport_failed"), source_order()])
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=reader,
        _target_reader=AsyncMock(return_value=missing_target()),
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == expected
    assert state.events[:2] == [("reserve", 2, 1), ("reserve", 2, 0)]
    assert reader.await_count == (2 if expected == "done" else 1)
    if expected == "done":
        assert len(state.reports) == 1
        assert state.run.progress_json["read_retry_count"] == 1
    else:
        assert not state.reports
        assert state.run.progress_json["pending_refs"] == [REF]


async def test_exhausted_netsuite_retry_yields_checkpoint_and_resumes_without_restarting_source_page():
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

    state = State()
    state.run.progress_json = {"read_retry_count": 3, "pending_refs": [REF], "last_source_id": 42}
    target = AsyncMock(side_effect=NetSuiteEvidenceError("read_transport_failed"))
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=AsyncMock(return_value=source_order()),
        _target_reader=target,
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == "budget"
    assert state.run.progress_json["pending_refs"] == [REF]
    assert state.run.progress_json["last_source_id"] == 42
    assert state.run.progress_json["last_read_failure"]["stage"] == "netsuite_order"
    assert not state.reports
    target.assert_awaited_once()
    resumed = await execute(state)
    assert resumed["termination_reason"] == "done" and REF in state.reports
    assert state.run.progress_json["last_source_id"] == 42
    assert state.run.progress_json["read_retry_count"] == 3


async def test_transient_read_without_time_for_backoff_remains_continuable():
    from app.services.transaction_ops.continuation import next_metadata
    from app.services.transaction_ops.source_reader import SourceReadError

    state = State()
    state.run.created_at = NOW
    state.run.deadline_at = NOW + timedelta(milliseconds=500)
    state.run.progress_json = {"processed": 1, "scan_count": 1}
    reader = AsyncMock(side_effect=SourceReadError("source_transport_failed"))
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=reader,
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == "budget"
    reader.assert_awaited_once()
    assert state.events == [("reserve", 2, 1)]
    assert state.run.progress_json["pending_refs"] == [REF]
    assert next_metadata(state.run, NOW)["continuation_part"] == 2


async def test_deadline_during_progress_finishes_budget_for_immediate_continuation():
    from app.services.transaction_ops.state_service import StateError

    state = State()
    state.run.deadline_at = NOW
    state.update_progress = AsyncMock(side_effect=StateError("run_lease_lost"))
    state.finish_run = AsyncMock(wraps=state.finish_run)

    result = await execute(state)

    assert result["status"] == "finished"
    assert result["termination_reason"] == "budget"
    assert state.run.status == "finished"
    state.finish_run.assert_awaited_once()
    assert state.finish_run.call_args.kwargs["lease_token"] == state.token
    assert state.events == []  # No provider calls or new spend after expiry.


@pytest.mark.parametrize("deadline_passed", [False, True])
async def test_lost_owner_yields_without_finishing_another_workers_run(deadline_passed):
    from app.services.transaction_ops.state_service import StateError

    state = State()
    state.run.deadline_at = NOW if deadline_passed else NOW + timedelta(minutes=1)
    state.update_progress = AsyncMock(side_effect=StateError("run_lease_lost"))
    state.finish_run = AsyncMock(side_effect=StateError("run_lease_lost"))

    result = await execute(state)

    assert result["status"] == "yielded"
    assert result["termination_reason"] == "stall"
    assert state.run.status == "running"
    assert state.finish_run.await_count == int(deadline_passed)
    assert state.events == []


async def test_deadline_finalization_does_not_hide_other_state_errors():
    from app.services.transaction_ops.state_service import StateError

    state = State()
    state.run.deadline_at = NOW
    state.update_progress = AsyncMock(side_effect=StateError("run_lease_lost"))
    state.finish_run = AsyncMock(side_effect=StateError("not_found"))

    with pytest.raises(StateError) as exc:
        await execute(state)
    assert exc.value.code == "not_found"


@pytest.mark.parametrize("replacement_status", ["running", "done", "budget", "error"])
async def test_expired_worker_cannot_report_or_continue_a_replacement_owners_result(replacement_status):
    from app.services.transaction_ops.state_service import StateError

    state = State()
    state.run.deadline_at = NOW

    async def replaced_during_progress(*args, **kwargs):
        state.run.lease_token = uuid4() if replacement_status == "running" else None
        state.run.status = "running" if replacement_status == "running" else "finished"
        state.run.termination_reason = None if replacement_status == "running" else replacement_status
        raise StateError("run_lease_lost")

    state.update_progress = AsyncMock(side_effect=replaced_during_progress)
    state.finish_run = AsyncMock(wraps=state.finish_run)
    state.get_run = AsyncMock(wraps=state.get_run)

    result = await execute(state)

    assert result["status"] == "yielded"
    assert result["termination_reason"] == "stall"
    state.finish_run.assert_not_awaited()
    assert state.get_run.call_args.kwargs == {"lock": True}
    assert state.events == []


async def test_direct_window_filters_other_entities_before_native_reads_and_uses_keyset():
    state = State(window=True)
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(uuid4()))
    first = deepcopy(source_order()["orders"][0])
    first["business_entity"] = "Another entity"
    first["number"] = "R200000001"
    second = deepcopy(source_order()["orders"][0])
    second["id"] = "2"
    page = AsyncMock(
        side_effect=[
            {"page_complete": True, "page": 1, "total_count": 2, "orders": [first], "next_page": 2},
            {"page_complete": True, "page": 1, "total_count": 1, "orders": [second], "next_page": None},
        ]
    )
    result = await execute(state, page=page)
    assert result["termination_reason"] == "done"
    assert list(state.reports) == [REF]
    assert state.events.count("target") == 1
    assert state.run.progress_json["outside_scope"] == 1
    assert page.call_args_list[1].kwargs["after_id"] == 1
    assert page.call_args_list[1].kwargs["page"] == 1
    assert page.call_args_list[0].kwargs["updated_before"] == NOW


async def test_changed_source_entity_stops_before_wrong_subsidiary_lookup():
    state = State()
    source = source_order()
    source["orders"][0]["business_entity"] = "Wrong subsidiary"
    result = await execute(state, source=source)
    assert result["termination_reason"] == "stall"
    assert "target" not in state.events


async def test_header_match_with_unknown_refunds_does_not_increment_matched_count():
    from tests.test_transaction_balance_report import evidence

    state = State()
    source, target, config, _, _ = evidence()
    state.run.config_snapshot.update(config)
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.00")
    await execute(state, source=source, target=target)
    assert state.run.progress_json["matched"] == 0
    assert state.run.progress_json["not_verified"] == 1


@pytest.mark.parametrize("stage", ["source_refunds", "netsuite_refunds"])
@pytest.mark.parametrize("failure", ["transport", "local_timeout", "deadline"])
async def test_refund_read_limit_yields_without_consuming_unread_order(stage, failure):
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
    from app.services.transaction_ops.source_reader import SourceReadError
    from tests.test_transaction_balance_report import evidence

    state = State(budget=1000)
    source, target, config, _, _ = evidence()
    state.run.config_snapshot.update(config)
    state.run.config_snapshot["mapping_json"]["solidus_refund_step_id"] = str(uuid4())
    state.run.progress_json = {"read_retry_count": 3, "pending_refs": [REF]}
    refund = {"order_reference": REF, "currency": "USD", "complete": True, "amount": "100.00"}
    source_refunds = AsyncMock(return_value=refund)
    native_refunds = AsyncMock(return_value=refund)
    current_time = NOW
    failed_reader = source_refunds if stage == "source_refunds" else native_refunds
    if failure == "deadline":

        async def expire(*args, **kwargs):
            nonlocal current_time
            current_time = state.run.deadline_at
            raise TimeoutError

        failed_reader.side_effect = expire
    elif failure == "local_timeout":
        failed_reader.side_effect = TimeoutError
    else:
        failed_reader.side_effect = (
            SourceReadError("source_transport_failed")
            if stage == "source_refunds"
            else NetSuiteEvidenceError("read_transport_failed")
        )
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: current_time,
        _enabled=AsyncMock(return_value=True),
        _order_mirror=AsyncMock(),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=source_refunds,
        _target_refunds_reader=native_refunds,
    )
    assert result["termination_reason"] == "budget"
    assert state.run.progress_json["pending_refs"] == [REF]
    assert state.run.progress_json["processed"] == 0
    if failure != "deadline":
        assert state.run.progress_json["last_read_failure"]["stage"] == stage
    failed_reader.assert_awaited_once()


@pytest.mark.parametrize("mode", ["match", "difference", "unavailable", "budget"])
async def test_runner_collects_refunds_with_reserved_reads_and_preserves_partial_evidence(mode):
    from tests.test_transaction_balance_report import evidence

    state = State(budget=15 if mode == "budget" else 1000)
    source, target, config, _, _ = evidence()
    state.run.config_snapshot.update(config)
    state.run.config_snapshot["mapping_json"]["solidus_refund_step_id"] = str(uuid4())
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.00")
    refund = {"order_reference": REF, "currency": "USD", "complete": True, "amount": "100.00"}
    source_refunds = AsyncMock(return_value=refund)
    native_refunds = AsyncMock(return_value={**refund, "amount": "99.00" if mode == "difference" else "100.00"})
    if mode == "unavailable":
        source_refunds.side_effect = ValueError("private upstream information")
    await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=source_refunds,
        _target_refunds_reader=native_refunds,
    )
    report = state.reports[REF]
    assert (
        report["balance"]["status"]
        == {"match": "matched", "difference": "difference", "unavailable": "incomplete", "budget": "incomplete"}[mode]
    )
    assert "private upstream" not in str(report)
    if mode == "budget":
        assert state.run.termination_reason == "budget"
        native_refunds.assert_not_awaited()
    else:
        # Derived, not spelled out: this is the refund read budget plus the OAuth
        # maintenance allowance, and the literal went stale the moment the budget moved.
        assert ("reserve", MAX_REFUND_CALLS + 3, 0) in state.events
        native_refunds.assert_awaited_once()


@pytest.mark.asyncio
async def test_reads_are_reserved_before_calls_and_missing_observation_is_durable():
    state = State()
    result = await execute(state)
    assert result["termination_reason"] == "done"
    # The fake order read makes no real provider calls, so all 7 of its data calls go back;
    # the 3 reserved for sign-in maintenance stay charged.
    assert state.events == [("reserve", 2, 1), "source", ("reserve", 10, 0), "target", ("settle", 10, 3)]
    assert state.reports[REF]["comparison"]["recommended_action"] == "propose_missing_sync"
    assert state.reports[REF]["source"]["total"] == "100"
    assert state.run.progress_json["processed"] == 1


@pytest.mark.asyncio
async def test_budget_exhaustion_preserves_unprocessed_reference_for_next_run():
    state = State(budget=1)
    result = await execute(state)
    assert result["termination_reason"] == "budget"
    assert "source" not in state.events and not state.reports
    assert state.run.progress_json["pending_refs"] == [REF]


@pytest.mark.asyncio
async def test_disable_or_duplicate_dispatch_cannot_start_provider_reads():
    state = State()
    result = await execute(state, enabled=False)
    assert result["termination_reason"] == "stall" and not state.events
    state = State()
    state.claimable = False
    await execute(state)
    assert not state.events


@pytest.mark.asyncio
async def test_feature_revocation_is_checked_again_before_the_next_provider_call():
    state = State()
    flags = AsyncMock(side_effect=[True, True, False])
    source = AsyncMock(return_value=source_order())
    target = AsyncMock(return_value=missing_target())
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=source,
        _target_reader=target,
        _enabled=flags,
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == "stall"
    source.assert_awaited_once()
    target.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_error_does_not_turn_into_a_missing_transaction():
    state = State()
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=AsyncMock(side_effect=ValueError("sensitive upstream body")),
        _target_reader=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    assert result["termination_reason"] == "error"
    assert not state.reports and "sensitive" not in str(result)


@pytest.mark.asyncio
async def test_incomplete_lookup_is_reported_without_claiming_absence():
    state = State()
    raw = missing_target()
    raw["lookup"]["complete"] = False
    await execute(state, target=raw)
    assert state.reports[REF]["comparison"]["recommended_action"] == "gather_evidence"


@pytest.mark.asyncio
async def test_window_pages_are_checkpointed_and_only_complete_scan_finishes_done():
    state = State(window=True)
    page = AsyncMock(
        return_value={
            "page_complete": True,
            "page": 1,
            "page_size": 20,
            "total_count": 1,
            "pages": 1,
            "orders": source_order()["orders"],
            "next_page": None,
        }
    )
    result = await execute(state, page=page)
    assert result["termination_reason"] == "done" and state.run.progress_json["scan_complete"]
    assert state.run.progress_json["scan_count"] == 1
    page.assert_awaited_once()


@pytest.mark.asyncio
async def test_population_change_between_pages_stalls_instead_of_claiming_complete_scan():
    state = State(window=True)
    first = {
        "page_complete": True,
        "page": 1,
        "page_size": 20,
        "total_count": 21,
        "pages": 2,
        "orders": source_order()["orders"],
        "next_page": 2,
    }
    second = {**first, "page": 2, "total_count": 22, "orders": [], "next_page": None}
    result = await execute(state, page=AsyncMock(side_effect=[first, second]))
    assert result["termination_reason"] == "stall"
    assert state.run.progress_json["restart_scan"]


async def test_oversize_evidence_is_flagged_without_stalling_the_scan():
    import json

    from app.schemas.transaction_runs import _bounded_json
    from tests.test_transaction_ops_netsuite_actions import target as example_target

    class BoundedState(State):
        async def record_finding(self, *args, **kwargs):
            _bounded_json(args[4])
            await super().record_finding(*args, **kwargs)

    state = BoundedState()
    source = source_order()
    source["orders"][0].update(total="50000", item_total="50000")
    source["orders"][0]["line_items"] = [
        {"id": str(100000000000000000 + i), "quantity": "1", "price": "100", "total": "100", "adjustments": []}
        for i in range(500)
    ]
    target = example_target()
    target["order_reference"] = REF
    target["header"].update(subsidiary={"id": "1"}, subtotal="50000", total="50000", taxTotal="0", shippingCost="0")
    target["currency_metadata"]["symbol"] = "USD"
    base = target["lines"][0]
    target["lines"] = [
        {
            **base,
            "line": i + 1,
            "quantity": "1",
            "amount": "100",
            "custcol_fw_vat_amount": "0",
            "custcol_fw_solidus_line_id": str(100000000000000000 + i),
        }
        for i in range(500)
    ]
    targets = {**missing_target(), "orders": [target]}
    result = await execute(state, source=source, target=targets)
    assert result["termination_reason"] == "done"
    report = state.reports[REF]
    assert len(json.dumps(report).encode()) <= 65536
    assert report["comparison"]["recommended_action"] == "gather_evidence"
    assert report["evidence_limits"]["source_line_count"] == 500
    assert report["evidence_limits"]["code"] == "evidence_size_limit"
    assert report["source"]["lines_complete"] is False
    assert state.run.progress_json["processed"] == 1


@pytest.mark.parametrize("budget", [15, 1000])
async def test_commercial_credit_read_reserves_budget_before_native_access(monkeypatch, budget):
    from app.services.transaction_ops import commercial_credits
    from tests.test_transaction_balance_report import evidence

    state = State(budget=budget)
    source, target, config, _, _ = evidence()
    state.run.config_snapshot.update(config)
    source["orders"][0].update(
        state="complete",
        completed_at=NOW.isoformat(),
        item_total="100",
        ship_total="0",
        total="115",
        adjustment_total="15",
        adjustments=[
            {
                "id": "9",
                "adjustable_type": "Spree::Order",
                "adjustable_id": "100",
                "finalized": True,
                "label": "Reseller adjustment",
                "amount": "-5",
            }
        ],
    )
    target["orders"][0]["header"].update(total="120", taxTotal="20")

    async def read(*args):
        assert state.events[-1] == ("reserve", 20, 0)
        return None  # An unverified credit must retain the real difference.

    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr(commercial_credits, "read_commercial_credit_for_order", reader)
    await execute(state, source=source, target=target)
    assert state.reports[REF]["balance"]["amounts"]["order_total"]["delta"] == "-5.00"
    if budget == 15:
        reader.assert_not_awaited()
        assert state.run.termination_reason == "budget"
        assert state.run.progress_json["pending_refs"] == [REF]
    else:
        reader.assert_awaited_once()


def calls_then(count, result):
    """A reader that makes ``count`` real provider calls before returning."""
    from app.services.transaction_ops.call_meter import note_call

    async def read(*args, **kwargs):
        for _ in range(count):
            note_call()
        return result() if callable(result) else result

    return read


async def run_with(state, **readers):
    return await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
        **readers,
    )


@pytest.mark.asyncio
async def test_a_read_is_charged_what_it_used_not_the_worst_case_it_reserved():
    """The order read reserves 10: at most 7 data calls plus 3 for sign-in maintenance.
    Two real calls leave 5 data calls unused, which go back. The sign-in allowance stays
    charged, because a token refresh happens outside the metered path and cannot be seen."""
    state = State()
    await run_with(
        state,
        _source_reader=AsyncMock(return_value=source_order()),
        _target_reader=calls_then(2, missing_target),
    )
    assert ("reserve", 10, 0) in state.events
    assert ("settle", 10, 5) in state.events
    assert state.run.progress_json["metered_calls"] == 2


@pytest.mark.asyncio
async def test_a_read_that_uses_every_reserved_call_gets_nothing_back():
    state = State()
    await run_with(
        state,
        _source_reader=AsyncMock(return_value=source_order()),
        _target_reader=calls_then(9, missing_target),
    )
    # Nine is more than the 7 data calls reserved: never hand back a negative amount,
    # and never let a settle charge less than was actually made.
    assert ("settle", 10, 10) in state.events
    assert state.run.progress_json["metered_calls"] == 9


@pytest.mark.asyncio
async def test_a_read_that_fails_is_still_charged_only_what_it_sent():
    from app.services.transaction_ops.call_meter import note_call

    state = State()

    async def fails_after_one_call(*args, **kwargs):
        note_call()
        raise ValueError("upstream unavailable")

    await run_with(
        state,
        _source_reader=AsyncMock(return_value=source_order()),
        _target_reader=fails_after_one_call,
    )
    assert ("settle", 10, 4) in state.events
    assert state.run.progress_json["metered_calls"] == 1


@pytest.mark.asyncio
async def test_a_settle_that_fails_after_a_failed_read_leaves_the_reads_own_error():
    """Settling runs in a ``finally``. If it raised there, its error would replace the
    read's, and a caller that degrades on read failures (the refund read) would instead
    abort on a state error. The hold is charged in full at finish, so nothing is lost by
    letting the read's error through."""
    from app.services.transaction_ops.state_service import StateError

    async def fails(*args, **kwargs):
        raise ValueError("upstream unavailable")

    baseline = State()
    expected = await run_with(baseline, _source_reader=AsyncMock(return_value=source_order()), _target_reader=fails)

    state = State()
    state.settle_budget = AsyncMock(side_effect=StateError("run_lease_lost"))
    result = await run_with(state, _source_reader=AsyncMock(return_value=source_order()), _target_reader=fails)
    state.settle_budget.assert_awaited_once()
    assert result["termination_reason"] == expected["termination_reason"] == "error"


@pytest.mark.asyncio
async def test_a_retry_is_paid_by_its_own_reservation_not_again_by_the_first_hold():
    """A retry reserves its own full cost. Its sends are covered there, so they must not
    also shrink what the first attempt hands back, or the same calls are charged twice."""
    from app.services.transaction_ops.call_meter import note_call
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

    attempts = []

    async def times_out_then_succeeds(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            for _ in range(3):
                note_call()
            raise NetSuiteEvidenceError("read_timeout")
        for _ in range(2):
            note_call()
        return missing_target()

    state = State()
    await run_with(state, _source_reader=AsyncMock(return_value=source_order()), _target_reader=times_out_then_succeeds)
    assert len(attempts) == 2
    # First attempt: 3 of 7 data calls sent, so 4 come back; the retry's 10 are spent as reserved.
    assert ("reserve", 10, 0) in state.events
    assert ("settle", 10, 6) in state.events
    assert state.run.progress_json["metered_calls"] == 5
