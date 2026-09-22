"""Model-issued ns_getRecordTypeMetadata calls get the same 1h scoped cache the write
validator already uses. Staging showed the call at 16.5 s p50 in 42 turns with the
cache bypassed, because only write_validation went through record_metadata_service.

The cache is at the dispatcher, keyed by tenant, actor, connector, credential revision
and the full tool input — never by record type or NetSuite account alone, which would
mix permissions or tenants.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat import record_metadata_service as rms
from app.services.chat.tools import _execute_external_tool

TENANT, ACTOR, CONN = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
META = {"fields": [{"id": "tranid", "label": "Number", "type": "text"}], "sublists": {}}


def _connector(**over):
    base = dict(
        id=CONN, provider="netsuite", is_enabled=True, status="active",
        server_url="https://1234567.suitetalk.api.netsuite.com", encrypted_credentials="enc",
        auth_type="oauth2", metadata_json={}, updated_at="2026-09-22T00:00:00",
    )  # fmt: skip
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _fresh_cache():
    rms.clear_metadata_cache()
    yield
    rms.clear_metadata_cache()


@pytest.fixture
def remote(monkeypatch):
    call = AsyncMock(return_value=dict(META))
    monkeypatch.setattr("app.services.mcp_client_service.call_external_mcp_tool", call)
    monkeypatch.setattr("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=_connector()))
    return call


async def _meta(actor=ACTOR, tenant=TENANT, record_type="invoice", tool="ns_getRecordTypeMetadata"):
    return await _execute_external_tool(CONN, tool, {"recordType": record_type}, tenant, AsyncMock(), actor_id=actor)


async def test_second_identical_lookup_is_served_from_cache(remote):
    first = await _meta()
    second = await _meta()
    assert remote.await_count == 1
    assert first["fields"] == META["fields"] and second["fields"] == META["fields"]
    assert first is not second  # a copy each time: post-processing mutates results


async def test_cached_result_still_gets_the_dispatchers_post_processing(remote):
    first = await _meta()
    second = await _meta()
    assert first["verified_connection_scope"]["account_id"] == "1234567"
    assert second["verified_connection_scope"] == first["verified_connection_scope"]


async def test_cache_is_per_actor_per_tenant_and_per_record_type(remote):
    await _meta()
    await _meta(actor=uuid.uuid4())
    await _meta(tenant=uuid.uuid4())
    await _meta(record_type="salesorder")
    assert remote.await_count == 4


async def test_a_credential_or_config_change_invalidates(remote, monkeypatch):
    await _meta()
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(return_value=_connector(encrypted_credentials="rotated")),
    )
    await _meta()
    assert remote.await_count == 2


async def test_errors_are_never_cached(remote):
    remote.return_value = {"error": "boom"}
    await _meta()
    remote.return_value = dict(META)
    result = await _meta()
    assert remote.await_count == 2 and result["fields"] == META["fields"]


async def test_without_an_actor_nothing_is_cached(remote):
    await _meta(actor=None)
    await _meta(actor=None)
    assert remote.await_count == 2


async def test_other_tools_are_untouched(remote):
    await _meta(tool="ns_getSubsidiaries")
    await _meta(tool="ns_getSubsidiaries")
    assert remote.await_count == 2


async def test_clear_metadata_cache_clears_the_raw_cache_too(remote):
    await _meta()
    rms.clear_metadata_cache()
    await _meta()
    assert remote.await_count == 2
