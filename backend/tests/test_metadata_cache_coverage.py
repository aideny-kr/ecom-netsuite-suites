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
# The live ns_getRecordTypeMetadata shape (record_metadata_service._parse_properties_shape).
META = {
    "success": True,
    "metadata": {"type": "object", "properties": {"tranid": {"title": "Number", "type": "string"}}},
}


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
    assert first["metadata"] == META["metadata"] and second["metadata"] == META["metadata"]
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
    assert remote.await_count == 2 and result["metadata"] == META["metadata"]


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


# ── gate round 1 on #288 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        {"success": False, "message": "Temporary upstream failure"},
        {"isError": True, "content": [{"type": "text", "text": "boom"}]},
        {},
        {"success": True, "metadata": {"properties": "not a mapping"}},
        {"success": False, "metadata": META["metadata"]},
        {"fields": [{"id": "tranid"}], "sublists": {}},  # malformed legacy shape
    ],
)
async def test_only_a_response_the_validator_can_parse_is_cached(remote, failure):
    """Caching is an allow-list: what the write validator's own parser accepts as
    metadata, from a response that does not declare itself failed. Anything else is
    fetched again next time, however it is shaped."""
    remote.return_value = failure
    await _meta()
    await _meta()
    assert remote.await_count == 2


async def _validate(record_type="invoice"):
    from app.services.chat.tools import _make_ext_tool_name

    return await rms.get_record_metadata(
        record_type=record_type,
        mutation_tool_name=_make_ext_tool_name(CONN, "ns_updateRecord"),
        tenant_id=TENANT,
        actor_id=ACTOR,
        correlation_id="c",
        db=AsyncMock(),
        session_id="s",
    )


@pytest.fixture
def validator_dispatch(monkeypatch):
    """get_record_metadata's execute_tool_call, reduced to the dispatcher it reaches."""
    import json

    async def dispatch(*, tool_name, tool_input, tenant_id, actor_id, **_):
        return json.dumps(
            await _execute_external_tool(
                CONN, "ns_getRecordTypeMetadata", tool_input, tenant_id, AsyncMock(), actor_id=actor_id
            )
        )

    monkeypatch.setattr(rms, "execute_tool_call", dispatch)


async def test_the_write_validator_fetches_live_even_after_a_model_lookup(remote, validator_dispatch):
    """Its own 1h cache is stamped when it fetches; a hit on the model's cache would
    restamp an hour-old response as new and stretch staleness towards two hours."""
    await _meta()
    meta = await _validate()
    assert meta is not None and meta.spec_for("tranid")
    assert remote.await_count == 2


async def test_the_write_validator_does_not_seed_the_model_cache(remote, validator_dispatch):
    await _validate()
    await _meta()
    assert remote.await_count == 2


async def test_a_cache_hit_says_so_and_the_audit_records_it(remote, monkeypatch):
    from app.services.chat import external_tool_audit

    first = await _meta()
    second = await _meta()
    assert "served_from_cache" not in first
    assert second["served_from_cache"]["age_seconds"] >= 0

    events = []

    async def capture(**kw):
        events.append(kw)

    monkeypatch.setattr(external_tool_audit, "append_event", capture)
    await external_tool_audit.audited_external_call(
        execute=_meta, tenant_id=TENANT, actor_id=ACTOR, actor_type="user", correlation_id="c", session_id="s",
        connector_id=CONN, tool_name="ns_getRecordTypeMetadata", params={}, human_approved=False,
    )  # fmt: skip
    assert events[-1]["action"] == "tool.executed"
    assert set(events[-1]["payload"]["served_from_cache"]) == {"age_seconds"}


async def test_a_remote_server_cannot_claim_its_answer_came_from_our_cache(remote):
    remote.return_value = {**META, "served_from_cache": {"age_seconds": 999}}
    first = await _meta()
    assert "served_from_cache" not in first


# ── gate round 2 on #288 ───────────────────────────────────────────────────


async def test_a_response_carrying_any_error_key_is_never_cached(remote):
    remote.return_value = {"error": "", **META}
    await _meta()
    await _meta()
    assert remote.await_count == 2


async def test_the_audit_hash_is_the_same_for_a_live_answer_and_its_cached_copy(remote, monkeypatch):
    """The age marker is provenance, not content: identical NetSuite metadata must hash
    identically whether it came live or from the cache, and across cache hits."""
    from app.services.chat import external_tool_audit

    events = []

    async def capture(**kw):
        events.append(kw)

    monkeypatch.setattr(external_tool_audit, "append_event", capture)
    common = dict(
        tenant_id=TENANT, actor_id=ACTOR, actor_type="user", correlation_id="c", session_id="s",
        connector_id=CONN, tool_name="ns_getRecordTypeMetadata", params={}, human_approved=False,
    )  # fmt: skip
    await external_tool_audit.audited_external_call(execute=_meta, **common)
    await external_tool_audit.audited_external_call(execute=_meta, **common)
    live, cached = (e["payload"] for e in events if e["action"] == "tool.executed")
    assert cached["served_from_cache"] is not None and live["served_from_cache"] is None
    assert cached["result_sha256"] == live["result_sha256"]
    assert cached["result_bytes"] == live["result_bytes"]
