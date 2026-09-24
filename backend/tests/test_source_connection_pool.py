"""Connection reuse keeps fresh evidence, authorization and DNS boundaries."""

import socket
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core.encryption import encrypt_credentials
from app.services.transaction_ops import source_reader
from app.services.transaction_ops.read_transport import CollectionTransport, collection_transport, current_transport
from tests.test_transaction_source_snapshot import REF, seed


@pytest.fixture
def upstream(monkeypatch):
    requests, transports = [], []
    dns = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr("app.services.public_http.resolve_addresses", dns)

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"number": REF, "currency": "USD", "total": str(100 + len(requests))})

    def transport(**options):
        result = httpx.MockTransport(handle)
        transports.append(result)
        return result

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    return requests, transports, dns


async def test_reuse_reads_new_values_and_rechecks_dns_and_revocation(db, admin_user, upstream):
    tenant = admin_user[0].tenant_id
    conn, _ = await seed(db, tenant)
    requests, transports, dns = upstream
    pool = CollectionTransport()
    try:
        with collection_transport(pool):
            first = await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            client = next(iter(pool.clients.values()))
            second = await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            assert first["orders"][0]["total"] != second["orders"][0]["total"]
            assert len(transports) == 1 and not client.is_closed
            assert dns.await_count == 2 and len(requests) == 2
            assert all(r.url.host == "93.184.216.34" for r in requests)
            assert all(r.headers["host"] == "private-direct-access.frame.work" for r in requests)
            conn.status = "revoked"
            await db.flush()
            with pytest.raises(source_reader.SourceReadError, match="source_not_found"):
                await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            assert len(requests) == 2
        assert current_transport() is None
    finally:
        await pool.aclose()
    assert client.is_closed and not pool.clients


async def test_credential_rotation_gets_new_client_and_other_tenant_cannot_reuse_it(db, admin_user, upstream):
    tenant = admin_user[0].tenant_id
    conn, _ = await seed(db, tenant)
    requests, transports, _ = upstream
    pool = CollectionTransport()
    try:
        with collection_transport(pool):
            await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            old_client = next(iter(pool.clients.values()))
            old_client.cookies.set("old-session", "must-not-cross")
            conn.encrypted_credentials = encrypt_credentials(
                {
                    "base_url": source_reader._FRAMEWORK_BASE,
                    "api_profile": "framework_sync",
                    "auth_type": "api_key",
                    "header_name": "X-Store-Token",
                    "token": "rotated",
                }
            )
            await db.flush()
            await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            assert len(transports) == 2
            assert requests[0].headers["x-store-token"] == "test"
            assert requests[1].headers["x-store-token"] == "rotated"
            assert "cookie" not in requests[1].headers
            with pytest.raises(source_reader.SourceReadError, match="source_not_found"):
                await source_reader.read_framework_order(db, uuid4(), None, REF, source_connection_id=conn.id)
            assert len(requests) == 2
    finally:
        clients = list(pool.clients.values())
        await pool.aclose()
    assert all(client.is_closed for client in clients)


async def test_changed_private_dns_is_rejected_even_with_a_pooled_client(db, admin_user, upstream):
    tenant = admin_user[0].tenant_id
    conn, _ = await seed(db, tenant)
    requests, _, dns = upstream
    pool = CollectionTransport()
    try:
        with collection_transport(pool):
            await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            dns.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)))
            with pytest.raises(source_reader.SourceReadError, match="source_transport_failed"):
                await source_reader.read_framework_order(db, tenant, None, REF, source_connection_id=conn.id)
            assert len(requests) == 1
    finally:
        await pool.aclose()


async def test_pool_evicts_and_closes_old_scopes(upstream):
    pool = CollectionTransport()
    clients = []
    try:
        for _ in range(5):
            clients.append(await pool.get_public((str(uuid4()),), source_reader._FRAMEWORK_BASE))
        assert len(pool.clients) == 4 and clients[0].is_closed
        assert all(not client.is_closed for client in clients[1:])
    finally:
        await pool.aclose()
    assert all(client.is_closed for client in clients)


async def test_runner_closes_source_pool_on_read_failure(upstream):
    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_ops_runner import NOW, State

    state = State()
    clients = []

    async def fail(*args, **kwargs):
        pool = current_transport()
        assert pool is not None
        clients.append(await pool.get_public((str(state.tenant),), source_reader._FRAMEWORK_BASE))
        raise source_reader.SourceReadError("invalid_source_response")

    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _source_reader=fail,
        _enabled=AsyncMock(return_value=True),
    )
    assert result["termination_reason"] == "error"
    assert clients and all(client.is_closed for client in clients)
    assert current_transport() is None
