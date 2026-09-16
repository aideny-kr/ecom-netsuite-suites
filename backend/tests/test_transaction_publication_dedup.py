"""Real Redis atomic publication reservation; broker send stays local to the fixture."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from redis import Redis

from app.services.transaction_ops import scheduler as mod


@pytest.fixture
def broker():
    url = mod.celery_app.conf.broker_url
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "redis"}, "Requires test Redis"
    client = Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    tenant, run = uuid4(), uuid4()
    keys = []

    def key(t=tenant, r=run):
        value = f"transaction-investigation:published:{t}:{r}"
        keys.append(value)
        return value

    @contextmanager
    def connection(**kwargs):
        assert kwargs["transport_options"]["socket_timeout"] == mod._BROKER_IO_TIMEOUT
        yield SimpleNamespace(default_channel=SimpleNamespace(client=client))

    app = SimpleNamespace(connection_for_write=connection, send_task=Mock())
    try:
        yield app, client, tenant, run, key
    finally:
        for value in set(keys):
            client.delete(value)
        client.close()


def test_concurrent_publishers_send_once_and_expiry_allows_recovery(broker):
    app, client, tenant, run, key = broker
    reservation = key()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: mod.publish_investigation(tenant, run, app=app), range(16)))
    assert results.count(True) == 1 and results.count(False) == 15
    app.send_task.assert_called_once()
    assert 0 < client.ttl(reservation) <= mod._PUBLICATION_COOLDOWN == 300
    # Expire our exact fixture key; the pending DB run can be published again.
    client.expire(reservation, 0)
    assert mod.publish_investigation(tenant, run, app=app)
    assert app.send_task.call_count == 2


def test_tenant_and_run_scope_do_not_suppress_distinct_work(broker):
    app, _, tenant, run, key = broker
    second_tenant, second_run = uuid4(), uuid4()
    for t, r in ((tenant, run), (second_tenant, run), (tenant, second_run)):
        key(t, r)
        assert mod.publish_investigation(t, r, app=app)
    assert app.send_task.call_count == 3


def test_ambiguous_send_retains_bounded_reservation(broker):
    app, client, tenant, run, key = broker
    reservation = key()
    app.send_task.side_effect = TimeoutError("Possibly published")
    with pytest.raises(TimeoutError):
        mod.publish_investigation(tenant, run, app=app)
    assert mod.publish_investigation(tenant, run, app=app) is False
    assert app.send_task.call_count == 1
    assert 0 < client.ttl(reservation) <= 300


async def test_suppression_is_reported_without_claiming_dispatch(monkeypatch):
    monkeypatch.setattr(mod, "publish_investigation", Mock(return_value=False))
    stats = {"dispatched": 0, "dispatch_failed": 0}
    await mod._dispatch(uuid4(), uuid4(), stats)
    assert stats == {"dispatched": 0, "dispatch_failed": 0, "deduplicated": 1}
