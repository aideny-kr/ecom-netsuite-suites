"""Unit tests for the server-side editable-slot option-source registry.

The registry is the authorization boundary: a model may HINT a field name via
`ask_user`, but every VALUE offered to the human comes from a server-executed,
code-reviewed tool call — never from the model. These tests prove that
boundary in isolation, before any wiring into base_agent.py's intercept.
"""

import json

import pytest

from app.services.chat import slot_option_sources as svc

EXT = "ext__" + "a" * 32 + "__ns_createRecord"


def test_is_option_sourced_true_for_registered_field():
    assert svc.is_option_sourced("subsidiary") is True


def test_is_option_sourced_false_for_unregistered_field():
    """A model-named field the registry has never heard of must not resolve
    to any options — this is what stops a model from minting its own slot
    by simply naming a field that happens to exist in metadata."""
    assert svc.is_option_sourced("location") is False
    assert svc.is_option_sourced("companyname") is False
    assert svc.is_option_sourced("") is False


@pytest.mark.asyncio
async def test_subsidiary_fetch_calls_ns_get_subsidiaries_on_the_same_connector(monkeypatch):
    seen = {}

    async def fake_exec(**kwargs):
        seen.update(kwargs)
        return json.dumps({"subsidiaries": [{"id": "1", "name": "Framework Inc"}]})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    options = await svc.fetch_options(
        "subsidiary",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert seen["tool_name"].endswith("ns_getSubsidiaries")
    assert seen["tool_name"].startswith("ext__" + "a" * 32)
    assert options == [{"value": "1", "label": "Framework Inc"}]


@pytest.mark.asyncio
async def test_unregistered_field_returns_empty_without_any_fetch(monkeypatch):
    calls = {"n": 0}

    async def fake_exec(**kwargs):
        calls["n"] += 1
        return json.dumps({"subsidiaries": []})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    options = await svc.fetch_options(
        "location",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert options == []
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_zero_options_from_a_real_fetch_returns_empty_not_error(monkeypatch):
    async def fake_exec(**kwargs):
        return json.dumps({"subsidiaries": []})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    options = await svc.fetch_options(
        "subsidiary",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert options == []


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty_not_raise(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("MCP down")

    monkeypatch.setattr(svc, "execute_tool_call", boom)
    options = await svc.fetch_options(
        "subsidiary",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert options == []


@pytest.mark.asyncio
async def test_malformed_response_returns_empty_not_raise(monkeypatch):
    async def fake_exec(**kwargs):
        return json.dumps({"error": "no subsidiaries endpoint"})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    options = await svc.fetch_options(
        "subsidiary",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert options == []


@pytest.mark.asyncio
async def test_non_dict_subsidiary_entries_are_skipped_not_raised(monkeypatch):
    async def fake_exec(**kwargs):
        return json.dumps({"subsidiaries": ["not-a-dict", {"id": "1", "name": "Framework Inc"}]})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    options = await svc.fetch_options(
        "subsidiary",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )
    assert options == [{"value": "1", "label": "Framework Inc"}]
