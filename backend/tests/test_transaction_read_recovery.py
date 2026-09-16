from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.transaction_ops.metabase_reader import ReplicaReadError
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
from app.services.transaction_ops.read_recovery import ReadBudgetExhaustedError, read_with_recovery
from app.services.transaction_ops.source_reader import SourceReadError


async def invoke(factory, *, progress=None, reserve=None, remaining=None, retry_calls=6):
    progress = progress if progress is not None else {}
    reserve = reserve or AsyncMock(return_value=True)
    save, sleep = AsyncMock(), AsyncMock()
    result = await read_with_recovery(
        factory,
        retry_calls=retry_calls,
        progress=progress,
        reserve=reserve,
        save=save,
        remaining=remaining or (lambda: 60),
        sleep=sleep,
    )
    return result, reserve, save, sleep


@pytest.mark.parametrize(
    "error",
    [
        SourceReadError("source_transport_failed"),
        SourceReadError("source_rate_limited"),
        ReplicaReadError("replica_transport_failed"),
        NetSuiteEvidenceError("read_timeout"),
        NetSuiteEvidenceError("upstream_http_429"),
        NetSuiteEvidenceError("upstream_http_503"),
        httpx.ReadTimeout("private response must not be stored"),
    ],
)
async def test_transient_read_reserves_again_and_preserves_checkpoint(error):
    progress = {"pending_refs": ["R100000001"], "last_source_id": 42}
    factory = AsyncMock(side_effect=[error, {"complete": True}])
    result, reserve, save, sleep = await invoke(factory, progress=progress)
    assert result == {"complete": True}
    reserve.assert_awaited_once_with(6)
    assert progress["read_retry_count"] == 1 and progress["last_source_id"] == 42
    assert progress["pending_refs"] == ["R100000001"]
    save.assert_awaited_once()
    sleep.assert_awaited_once_with(1)


@pytest.mark.parametrize(
    "error",
    [
        SourceReadError("source_credentials_unavailable"),
        ReplicaReadError("replica_identity_ambiguous"),
        NetSuiteEvidenceError("upstream_http_400"),
        ValueError("sensitive response body"),
    ],
)
async def test_untrusted_or_permanent_failure_is_not_retried_or_leaked(error):
    factory, reserve, progress = AsyncMock(side_effect=error), AsyncMock(), {}
    with pytest.raises(type(error)):
        await invoke(factory, reserve=reserve, progress=progress)
    factory.assert_awaited_once()
    reserve.assert_not_awaited()
    assert "sensitive" not in str(progress) and "credentials" not in str(progress)


async def test_read_retries_are_bounded_across_a_saved_continuation():
    factory = AsyncMock(side_effect=ReplicaReadError("replica_transport_failed"))
    progress, reserve = {"read_retry_count": 2}, AsyncMock(return_value=True)
    with pytest.raises(ReplicaReadError):
        await invoke(factory, progress=progress, reserve=reserve)
    assert factory.await_count == 2 and progress["read_retry_count"] == 3
    reserve.assert_awaited_once_with(6)


async def test_insufficient_budget_never_replays_provider_read():
    factory, reserve = (
        AsyncMock(side_effect=ReplicaReadError("replica_transport_failed")),
        AsyncMock(return_value=False),
    )
    with pytest.raises(ReadBudgetExhaustedError):
        await invoke(factory, reserve=reserve)
    factory.assert_awaited_once()


async def test_expired_deadline_never_starts_provider_read():
    factory = AsyncMock()
    with pytest.raises(TimeoutError):
        await invoke(factory, remaining=lambda: 0)
    factory.assert_not_awaited()


async def test_lost_lease_is_not_retried_or_followed_by_a_progress_write():
    from app.services.transaction_ops.state_service import StateError

    reserve, save = AsyncMock(), AsyncMock()
    with pytest.raises(StateError):
        await read_with_recovery(
            AsyncMock(side_effect=StateError("run_lease_lost")),
            retry_calls=6,
            progress={},
            reserve=reserve,
            save=save,
            remaining=lambda: 60,
        )
    reserve.assert_not_awaited()
    save.assert_not_awaited()


async def test_unregistered_read_does_not_retry():
    factory, reserve = AsyncMock(side_effect=ReplicaReadError("replica_transport_failed")), AsyncMock()
    with pytest.raises(ReplicaReadError):
        await invoke(factory, retry_calls=0, reserve=reserve)
    reserve.assert_not_awaited()
    factory.assert_awaited_once()


async def test_retry_checkpoint_commits_before_another_provider_call():
    progress, events = {}, []

    async def factory():
        events.append("read")
        if len(events) == 1:
            raise ReplicaReadError("replica_transport_failed")
        return True

    async def reserve(cost):
        events.append(("reserve", cost))
        return True

    async def save():
        events.append(("save", progress["read_retry_count"]))

    assert await read_with_recovery(
        factory, retry_calls=6, progress=progress, reserve=reserve, save=save, remaining=lambda: 60, sleep=AsyncMock()
    )
    assert events == ["read", ("reserve", 6), ("save", 1), "read"]
