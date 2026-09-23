from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops.run_timing import RunTiming


async def test_timing_keeps_failures_and_state_semantics_without_payloads():
    progress = {"timing_ms": {"old": {"calls": 100}}}
    times = iter([0, 1, 1.25, 2, 2.6])
    timing = RunTiming(progress, clock=lambda: next(times))
    state = AsyncMock()
    state.record_finding.side_effect = RuntimeError("private provider payload")
    wrapped = timing.state(state)
    with pytest.raises(RuntimeError):
        await wrapped.record_finding("unchanged")
    with timing.measure("untrusted reference"):
        pass
    state.record_finding.assert_awaited_once_with("unchanged")
    assert progress["timing_ms"] == {
        "record_finding": {"calls": 1, "total": 250, "max": 250},
        "other": {"calls": 1, "total": 600, "max": 600},
    }
    assert wrapped.StateError is state.StateError


async def test_cancellation_is_not_swallowed():
    import asyncio

    progress = {}
    timing = RunTiming(progress)
    with pytest.raises(asyncio.CancelledError), timing.measure("netsuite_order"):
        raise asyncio.CancelledError
    assert progress["timing_ms"]["netsuite_order"]["calls"] == 1
