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
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.connection import Connection
from app.models.tenant import Tenant
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


class TestStripeSyncGuardTenantAndProviderScoping:
    """Real-Postgres check of the pre-flight guard's filter shape -- the guard
    previously filtered only id+status, so a connection_id belonging to a
    DIFFERENT tenant (or even a different provider) would still pass, same
    class of bug as connection_service.get_connection guards against. Mirrors
    that helper's predicates (id + tenant_id); provider is added on top since
    this guard, unlike get_connection, is provider-specific.

    Also proves this guard is what turns the fan-out's now-widened dispatch
    for an `error`-status connection (see test_sync_all_fanout.py) into an
    actual raise: ACTIVE_CONNECTION_STATUSES (active/healthy only) rejects
    'error' here, same reliability class as the 2026-07-29 incident.

    Uses a real Postgres session on a rolled-back sub-transaction (same
    isolation pattern as test_ingestion.py's TestCountActiveStripeConnectionsDbBacked)
    rather than the ``_FakeDB`` above, which ignores its query args entirely
    and so can't exercise the real WHERE-clause shape.
    """

    @pytest.fixture
    def sync_db(self):
        from app.workers.base_task import sync_engine

        with sync_engine.connect() as conn:
            trans = conn.begin()
            session = Session(bind=conn)
            try:
                yield session
            finally:
                session.close()
                trans.rollback()

    @staticmethod
    def _make_tenant(session) -> Tenant:
        tenant = Tenant(
            name="Stripe Task Guard Test Corp",
            slug=f"test-{uuid.uuid4().hex[:8]}",
            plan="free",
            plan_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
            is_active=True,
        )
        session.add(tenant)
        session.flush()
        return tenant

    @staticmethod
    def _make_connection(session, tenant_id, *, provider="stripe", status="active") -> Connection:
        conn = Connection(
            tenant_id=tenant_id,
            provider=provider,
            label=provider,
            status=status,
            encrypted_credentials="encrypted_blob",
        )
        session.add(conn)
        session.flush()
        return conn

    @staticmethod
    def _patch_real_session(monkeypatch, sync_db):
        @contextmanager
        def fake_tenant_session(_tenant_id):
            yield sync_db

        monkeypatch.setattr("app.workers.tasks.stripe_sync.tenant_session", fake_tenant_session)

        calls: list[tuple] = []

        def fake_sync_stripe(db, connection_id, tenant_id):
            calls.append((connection_id, tenant_id))
            return {"ok": True}

        monkeypatch.setattr("app.workers.tasks.stripe_sync.sync_stripe", fake_sync_stripe)
        return calls

    def test_guard_rejects_connection_belonging_to_a_different_tenant(self, monkeypatch, sync_db):
        """A connection_id that's real, active, and the right provider but
        belongs to ANOTHER tenant must never be usable just because the id
        matches -- the guard must scope by tenant_id too."""
        owner_tenant = self._make_tenant(sync_db)
        other_tenant = self._make_tenant(sync_db)
        conn = self._make_connection(sync_db, owner_tenant.id)

        calls = self._patch_real_session(monkeypatch, sync_db)

        with pytest.raises(StripeSyncFailedError, match="No active Stripe connection found"):
            stripe_sync(tenant_id=str(other_tenant.id), connection_id=str(conn.id))

        assert calls == []  # sync_stripe must never run against the wrong tenant's connection

    def test_guard_rejects_wrong_provider(self, monkeypatch, sync_db):
        """A connection_id that's active and belongs to the right tenant but is
        a different provider's connection (e.g. netsuite) must be rejected --
        the guard must scope by provider too."""
        tenant = self._make_tenant(sync_db)
        conn = self._make_connection(sync_db, tenant.id, provider="netsuite")

        calls = self._patch_real_session(monkeypatch, sync_db)

        with pytest.raises(StripeSyncFailedError, match="No active Stripe connection found"):
            stripe_sync(tenant_id=str(tenant.id), connection_id=str(conn.id))

        assert calls == []

    def test_guard_rejects_error_status_connection(self, monkeypatch, sync_db):
        """Right tenant, right provider, but status='error' must still be
        rejected -- this is the exact scenario the widened fan-out now
        dispatches for (item 1): the guard's raise is what makes that dispatch
        produce a visible failed job row instead of a silent no-op."""
        tenant = self._make_tenant(sync_db)
        conn = self._make_connection(sync_db, tenant.id, status="error")

        calls = self._patch_real_session(monkeypatch, sync_db)

        with pytest.raises(StripeSyncFailedError, match="No active Stripe connection found"):
            stripe_sync(tenant_id=str(tenant.id), connection_id=str(conn.id))

        assert calls == []

    def test_guard_accepts_matching_tenant_provider_and_status(self, monkeypatch, sync_db):
        """Sanity check: the added predicates don't reject a legitimately
        matching connection."""
        tenant = self._make_tenant(sync_db)
        conn = self._make_connection(sync_db, tenant.id)

        calls = self._patch_real_session(monkeypatch, sync_db)

        out = stripe_sync(tenant_id=str(tenant.id), connection_id=str(conn.id))

        assert out == {"ok": True}
        assert calls == [(str(conn.id), str(tenant.id))]
