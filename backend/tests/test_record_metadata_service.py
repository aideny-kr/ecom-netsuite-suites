import json

import pytest

from app.services.chat import record_metadata_service as svc

EXT = "ext__" + "a" * 32 + "__ns_createRecord"

_META = {
    "fields": [
        {"name": "companyname", "label": "Company Name", "mandatory": False, "type": "text"},
        {"name": "subsidiary", "label": "Primary Subsidiary", "mandatory": True, "type": "select"},
    ],
    "sublists": [
        {"name": "line", "fields": [{"name": "account", "label": "Account", "mandatory": True, "type": "select"}]}
    ],
}


@pytest.fixture(autouse=True)
def _clear():
    svc.clear_metadata_cache()
    yield
    svc.clear_metadata_cache()


@pytest.mark.asyncio
async def test_parses_required_fields_and_line_fields(monkeypatch):
    async def fake_exec(**kwargs):
        assert kwargs["tool_name"].endswith("ns_getRecordTypeMetadata")
        return json.dumps(_META)

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    meta = await svc.get_record_metadata(
        record_type="customer",
        mutation_tool_name=EXT,
        tenant_id=None,
        actor_id=None,
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert meta is not None
    assert [f.name for f in meta.fields if f.required] == ["subsidiary"]
    assert [f.name for f in meta.line_fields if f.required] == ["account"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    async def fake_exec(**kwargs):
        calls["n"] += 1
        return json.dumps(_META)

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    kw = dict(
        record_type="customer",
        mutation_tool_name=EXT,
        tenant_id=None,
        actor_id=None,
        correlation_id="c",
        db=None,
        session_id="s",
    )
    await svc.get_record_metadata(**kw)
    await svc.get_record_metadata(**kw)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_failure_returns_none_not_empty(monkeypatch):
    """A failed lookup must be distinguishable from 'no required fields'."""

    async def boom(**kwargs):
        raise RuntimeError("MCP down")

    monkeypatch.setattr(svc, "execute_tool_call", boom)
    meta = await svc.get_record_metadata(
        record_type="customer",
        mutation_tool_name=EXT,
        tenant_id=None,
        actor_id=None,
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert meta is None


@pytest.mark.asyncio
async def test_error_response_returns_none(monkeypatch):
    """A well-formed JSON error envelope is a failure, not empty metadata."""

    async def fake_exec(**kwargs):
        return json.dumps({"error": "record type not found"})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    meta = await svc.get_record_metadata(
        record_type="customer",
        mutation_tool_name=EXT,
        tenant_id=None,
        actor_id=None,
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert meta is None


@pytest.mark.asyncio
async def test_missing_sublists_key_is_no_line_items_not_unknown(monkeypatch):
    """A genuinely absent 'sublists' key is a valid 'no line items' shape.

    Distinct from ``test_malformed_metadata_shape_returns_none_not_empty``'s
    ``sublists_none`` case, where the key is present but explicitly null —
    that is malformed and must be ``None``. Here the key is never sent at
    all, which real record types with no sublists are expected to do.
    """

    async def fake_exec(**kwargs):
        return json.dumps({"fields": [{"name": "companyname", "label": "Company Name", "mandatory": False}]})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    meta = await svc.get_record_metadata(
        record_type="customer",
        mutation_tool_name=EXT,
        tenant_id=None,
        actor_id=None,
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert meta is not None
    assert meta.line_fields == []


@pytest.mark.parametrize(
    "malformed",
    [
        {"fields": None, "sublists": []},
        {"fields": ["not-a-dict"], "sublists": []},
        {"fields": [], "sublists": ["nope"]},
        {"fields": [], "sublists": None},
    ],
    ids=[
        "fields_none",
        "fields_contains_non_dict",
        "sublists_contains_non_dict",
        "sublists_none",
    ],
)
@pytest.mark.asyncio
async def test_malformed_metadata_shape_returns_none_not_empty(monkeypatch, malformed):
    """Present-but-wrong-type payload shapes are 'unknown', never 'empty'.

    A response that parses as JSON but doesn't match the expected shape must
    not silently report "no required fields" — that reads as a validated,
    complete payload when the requirements were never actually read.
    """

    async def fake_exec(**kwargs):
        return json.dumps(malformed)

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    meta = await svc.get_record_metadata(
        record_type="customer",
        mutation_tool_name=EXT,
        tenant_id=None,
        actor_id=None,
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert meta is None
