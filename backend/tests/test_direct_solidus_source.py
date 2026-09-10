"""Direct Solidus preserves source completeness, identity and tenant controls."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.services.transaction_ops import source_reader as reader
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state import config_input


@pytest.fixture
def direct():
    tenant, identifier = uuid4(), uuid4()
    credentials = {
        "base_url": "https://private-direct-access.frame.work/api/",
        "auth_type": "api_key",
        "header_name": "X-Store-Token",
        "token": "secret",
        "api_profile": "framework_sync",
    }
    connection = SimpleNamespace(id=identifier, encrypted_credentials=encrypt_credentials(credentials))
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = connection
    return tenant, identifier, db


async def test_direct_exact_order_uses_configured_auth_and_safe_projection(direct):
    tenant, identifier, db = direct
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": 10, "number": "R123456789", "currency": "EUR", "total": "123.45", "password": "never-project"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        evidence = await reader.read_framework_order(
            db, tenant, None, "R123456789", source_connection_id=identifier, client=client
        )
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/sync/orders/R123456789"
    assert seen[0].headers["X-Store-Token"] == "secret"
    assert evidence["connection_id"] == str(identifier)
    assert evidence["source_transport"] == "solidus_direct"
    assert evidence["page_complete"] is True and evidence["window_complete"] is False
    assert "never-project" not in str(evidence)
    sql = str(
        db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert str(tenant) in sql and str(identifier) in sql and "solidus" in sql and "active" in sql


async def test_direct_wrong_identity_and_incomplete_pages_fail_closed(direct):
    tenant, identifier, db = direct
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"number": "R000000000"}))
    ) as client:
        with pytest.raises(reader.SourceReadError, match="order_identity_mismatch"):
            await reader.read_framework_order(
                db, tenant, None, "R123456789", source_connection_id=identifier, client=client
            )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"orders": []}))
    ) as client:
        with pytest.raises(reader.SourceReadError, match="incomplete_page"):
            await reader.read_framework_orders_page(
                db, tenant, None, datetime.now(timezone.utc), source_connection_id=identifier, client=client
            )


async def test_unavailable_source_never_contacts_provider(direct):
    tenant, identifier, db = direct
    db.execute.return_value.scalar_one_or_none.return_value = None
    upstream = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(reader.SourceReadError, match="source_not_found"):
            await reader.read_framework_order(
                db, tenant, None, "R123456789", source_connection_id=identifier, client=client
            )
    upstream.assert_not_called()


def test_scope_requires_exactly_one_source():
    identifier = uuid4()
    assert config_input(source_step_id=None, source_connection_id=identifier).source_connection_id == identifier
    for changes in ({"source_step_id": None}, {"source_connection_id": identifier}):
        with pytest.raises(ValidationError):
            config_input(**changes)


async def test_direct_config_checks_provider_profile_status_and_tenant(db, admin_user, admin_user_b):
    actor, _ = admin_user
    source = Connection(
        tenant_id=actor.tenant_id,
        provider="solidus",
        label="Direct",
        status="active",
        encrypted_credentials="not-read",
        encryption_key_version=1,
        metadata_json={"api_profile": "framework_sync"},
    )
    target = Connection(
        tenant_id=actor.tenant_id,
        provider="netsuite",
        label="NS",
        status="active",
        encrypted_credentials="not-read",
        encryption_key_version=1,
    )
    db.add_all([source, target])
    await db.flush()
    request = config_input(source_step_id=None, source_connection_id=source.id, netsuite_connection_id=target.id)
    scope = await state.create_config(db, actor.tenant_id, request, actor=actor)
    assert scope.source_step_id is None and scope.source_connection_id == source.id
    assert scope.schedule_enabled is False
    for field, value in (
        ("status", "revoked"),
        ("provider", "api"),
        ("metadata_json", {"api_profile": "solidus_rest"}),
        ("tenant_id", admin_user_b[0].tenant_id),
    ):
        original = getattr(source, field)
        setattr(source, field, value)
        await db.flush()
        with pytest.raises(state.StateError, match="source_unavailable"):
            await state._check_bindings(db, actor.tenant_id, request)
        setattr(source, field, original)
        await db.flush()
