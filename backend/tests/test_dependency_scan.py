"""Interrupted discovery resumes its exact page without losing old/new owners."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops import dependency_scan
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

A, B, C = "R100000001", "R100000002", "R100000003"
END = "2026-09-22T00:00:00+00:00"
OBSERVED = "2026-09-23T00:00:00+00:00"


def page(stream="transactions", *, more=False):
    return {
        "stream": stream,
        "page_complete": True,
        "scan_complete": not more,
        "changes": [{"record_keys": [["transaction", "17"]]}],
        "scope": {"window_end": END},
        "observed_at": OBSERVED,
        "next_cursor": [17] if more else None,
    }


def ports(value=None):
    return {
        "read_page": AsyncMock(return_value=value or page()),
        "read_owners": AsyncMock(return_value={"order_references": [A, B]}),
        "indexed_owners": AsyncMock(
            return_value={"order_references": [B, C], "has_more": False, "next_after_reference": None}
        ),
        "unobserved": AsyncMock(side_effect=lambda refs, **kw: refs),
    }


async def test_restart_keeps_page_and_both_removed_and_current_owners():
    progress, calls = {}, ports(page(more=True))
    await dependency_scan.advance(progress, **calls)
    durable = deepcopy(progress)
    await dependency_scan.advance(durable, **calls)
    durable = deepcopy(durable)
    await dependency_scan.advance(durable, **calls)
    assert durable["pending_refs"] == [A, B, C]
    assert durable["dependency_scan"]["after"] is None
    calls["read_page"].assert_awaited_once()
    calls["read_owners"].assert_awaited_once()
    calls["unobserved"].assert_awaited_once_with([A, B, C], since=END)
    # The runner drains the committed pending refs before advancing this stage.
    durable["pending_refs"] = []
    await dependency_scan.advance(durable, **calls)
    assert durable["dependency_scan"]["after"] == [17]
    assert "page" not in durable["dependency_scan"]
    assert not durable.get("dependency_scan_complete")


async def test_old_shared_owner_index_paginates_without_provider_repeats():
    calls, progress = ports(), {}
    calls["indexed_owners"].side_effect = [
        {"order_references": [A], "has_more": True, "next_after_reference": A},
        {"order_references": [C], "has_more": False, "next_after_reference": None},
    ]
    for _ in range(3):
        await dependency_scan.advance(progress, **calls)
    assert progress["pending_refs"] == [A, B]
    progress["pending_refs"] = []
    await dependency_scan.advance(progress, **calls)
    assert progress["pending_refs"] == [C]
    assert calls["indexed_owners"].call_args.kwargs["after_reference"] == A
    calls["read_page"].assert_awaited_once()
    calls["read_owners"].assert_awaited_once()


async def test_deletion_uses_saved_owners_and_requires_post_page_observation():
    calls = ports(page("deletions"))
    progress = {"dependency_scan": {"version": 1, "stream_index": 4, "after": None}}
    for _ in range(3):
        await dependency_scan.advance(progress, **calls)
    assert progress["pending_refs"] == [B, C]
    calls["read_owners"].assert_not_awaited()
    calls["unobserved"].assert_awaited_once_with([B, C], since=OBSERVED)
    progress["pending_refs"] = []
    await dependency_scan.advance(progress, **calls)
    assert not progress.get("dependency_scan_complete")
    await dependency_scan.advance(progress, **calls)
    assert progress["dependency_scan_complete"] and progress["destination_scan_complete"]


async def test_owner_failure_does_not_advance_saved_page_or_scan_cursor():
    calls, progress = ports(), {}
    await dependency_scan.advance(progress, **calls)
    before = deepcopy(progress)
    calls["read_owners"].side_effect = NetSuiteEvidenceError("dependency_owner_depth")
    with pytest.raises(NetSuiteEvidenceError):
        await dependency_scan.advance(progress, **calls)
    assert progress == before
    assert not progress.get("destination_scan_complete")


async def test_all_empty_streams_must_be_read_before_completion():
    calls, progress = ports(), {}

    async def empty(stream, after):
        return {**page(stream), "changes": []}

    calls["read_page"].side_effect = empty
    for _ in range(15):
        await dependency_scan.advance(progress, **calls)
        assert not progress.get("dependency_scan_complete")
    await dependency_scan.advance(progress, **calls)
    assert progress["dependency_scan_complete"] is True
    assert [c.args[0] for c in calls["read_page"].call_args_list] == list(dependency_scan.STREAMS)
    calls["read_owners"].assert_not_awaited()
    calls["indexed_owners"].assert_not_awaited()
