"""Stripe sync task honesty -- same reliability class as the NetSuite
deposit-sync 2026-07-29 incident (see test_netsuite_deposit_sync_task.py).

Unlike ``sync_netsuite_deposits``, ``sync_stripe`` doesn't return a graceful
"no active connection" result -- its internal connection lookup has no status
filter, so a connection that flipped to `error`/inactive would still be used
with whatever stale credentials it has. This task adds an explicit
active-connection guard BEFORE calling the service: no active connection for
`connection_id` is a total failure and must raise, so InstrumentedTask.on_failure
records the job `failed` instead of silently `completed`.

The payout-status-refresh pass inside ``sync_stripe`` is deliberately
fail-safe (its own per-row errors are swallowed and folded into
`payouts_refresh_errors` in the returned summary) -- those errors must NEVER
trip this guard. Only a missing/inactive connection (total failure) raises.
"""

import uuid
from contextlib import contextmanager

import pytest

from app.workers.tasks.stripe_sync import StripeSyncFailedError, stripe_sync


class _FakeConnection:
    def __init__(self, status="active"):
        self.id = uuid.uuid4()
        self.status = status


class _FakeExecuteResult:
    def __init__(self, connection):
        self._connection = connection

    def scalar_one_or_none(self):
        return self._connection


class _FakeDB:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, *_args, **_kwargs):
        return _FakeExecuteResult(self._connection)


def _patch(monkeypatch, *, connection, sync_result=None):
    @contextmanager
    def fake_tenant_session(_tenant_id):
        yield _FakeDB(connection)

    monkeypatch.setattr("app.workers.tasks.stripe_sync.tenant_session", fake_tenant_session)

    calls: list[tuple] = []

    def fake_sync_stripe(db, connection_id, tenant_id):
        calls.append((db, connection_id, tenant_id))
        return sync_result

    monkeypatch.setattr("app.workers.tasks.stripe_sync.sync_stripe", fake_sync_stripe)
    return calls


def test_no_active_connection_raises_total_failure(monkeypatch):
    calls = _patch(monkeypatch, connection=None)

    with pytest.raises(StripeSyncFailedError, match="No active Stripe connection found"):
        stripe_sync(tenant_id="t1", connection_id="c1")

    assert calls == []  # sync_stripe must never be called with no active connection


def test_partial_result_with_refresh_errors_still_completes(monkeypatch):
    """The refresh pass's own errors (payouts_refresh_errors) are deliberately
    fail-safe and must never fail the job."""
    summary = {
        "payouts_synced": 5,
        "payout_lines_synced": 12,
        "disputes_synced": 0,
        "payouts_refresh_checked": 4,
        "payouts_refresh_updated": 1,
        "payouts_refresh_errors": 2,
    }
    _patch(monkeypatch, connection=_FakeConnection(), sync_result=summary)

    out = stripe_sync(tenant_id="t1", connection_id="c1")

    assert out == summary


def test_healthy_sync_completes(monkeypatch):
    summary = {
        "payouts_synced": 10,
        "payout_lines_synced": 20,
        "disputes_synced": 1,
        "payouts_refresh_checked": 10,
        "payouts_refresh_updated": 0,
        "payouts_refresh_errors": 0,
    }
    _patch(monkeypatch, connection=_FakeConnection(), sync_result=summary)

    out = stripe_sync(tenant_id="t1", connection_id="c1")

    assert out == summary
