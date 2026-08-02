"""Nightly/hourly fan-out connection-status filtering.

2026-07-29 incident (see test_netsuite_deposit_sync_task.py): the Framework
NetSuite connection flipped to `error`, but `_find_active_netsuite_connections`
filtered `status IN ('active', 'healthy')` -- so once a connection goes dead,
the fan-out silently skips it FOREVER, and the child task's raise-on-failure
logic (fixed in netsuite_deposit_sync.py / stripe_sync.py) never even gets a
chance to run. The same gap existed in `_find_active_stripe_connections`.

Both fan-outs must now DISPATCH for an `error`-status connection too -- the
child task's guard/service-error path then raises, so InstrumentedTask
records a failed job row every night the connection stays dead (a visible,
actionable signal instead of silence). `revoked` and other intentionally-dead
statuses stay excluded -- there is no reasonable path back to health for
those, and dispatching them would just spam failures for a connection nobody
intends to reactivate.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.connection import Connection
from app.models.tenant import Tenant
from app.workers.tasks.netsuite_deposit_sync_all import _find_active_netsuite_connections
from app.workers.tasks.stripe_sync_all import _find_active_stripe_connections


@pytest.fixture
def sync_db():
    """Real Postgres session on a rolled-back sub-transaction -- same
    isolation pattern as test_ingestion.py's TestCountActiveStripeConnectionsDbBacked
    (these fan-out helpers take a synchronous ``Session`` -- the type Celery
    workers actually use via ``app.workers.base_task.sync_engine`` -- not the
    async ``db``/``tenant_a`` fixtures used elsewhere in this suite)."""
    from app.workers.base_task import sync_engine

    with sync_engine.connect() as conn:
        trans = conn.begin()
        session = Session(bind=conn)
        try:
            yield session
        finally:
            session.close()
            trans.rollback()


def _make_tenant(session) -> Tenant:
    tenant = Tenant(
        name="Fanout Status Test Corp",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        plan="free",
        plan_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        is_active=True,
    )
    session.add(tenant)
    session.flush()
    return tenant


def _make_connection(session, tenant_id, *, provider: str, status: str) -> Connection:
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


class TestNetsuiteFanoutDispatchableStatuses:
    def test_dispatches_error_status_skips_revoked(self, sync_db):
        active_tenant = _make_tenant(sync_db)
        error_tenant = _make_tenant(sync_db)
        revoked_tenant = _make_tenant(sync_db)
        _make_connection(sync_db, active_tenant.id, provider="netsuite", status="active")
        _make_connection(sync_db, error_tenant.id, provider="netsuite", status="error")
        _make_connection(sync_db, revoked_tenant.id, provider="netsuite", status="revoked")

        result = _find_active_netsuite_connections(sync_db)
        tenant_ids = {row["tenant_id"] for row in result}

        assert str(active_tenant.id) in tenant_ids
        assert str(error_tenant.id) in tenant_ids  # night-2 recurrence fix
        assert str(revoked_tenant.id) not in tenant_ids


class TestStripeFanoutDispatchableStatuses:
    def test_dispatches_error_status_skips_revoked(self, sync_db):
        active_tenant = _make_tenant(sync_db)
        error_tenant = _make_tenant(sync_db)
        revoked_tenant = _make_tenant(sync_db)
        _make_connection(sync_db, active_tenant.id, provider="stripe", status="active")
        _make_connection(sync_db, error_tenant.id, provider="stripe", status="error")
        _make_connection(sync_db, revoked_tenant.id, provider="stripe", status="revoked")

        result = _find_active_stripe_connections(sync_db)
        tenant_ids = {row["tenant_id"] for row in result}

        assert str(active_tenant.id) in tenant_ids
        assert str(error_tenant.id) in tenant_ids  # night-2 recurrence fix
        assert str(revoked_tenant.id) not in tenant_ids
