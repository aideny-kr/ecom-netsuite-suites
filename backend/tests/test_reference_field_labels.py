"""Human-readable labels for NetSuite reference fields on the confirmation card.

A NetSuite reference field arrives as ``{"id": "5"}``. Rendered verbatim, the
card asks an operator to approve `subsidiary {"id": "5"}` — a number they would
have to already know the meaning of, on the one screen whose entire job is
informed consent before a write.

TWO PROPERTIES THIS MUST NOT BREAK, both load-bearing:

1. DISPLAY ONLY. ``proposed_fields`` is what the card renders; ``tool_input``
   is what executes, under an HMAC. Labels must never reach the payload, or a
   display string would be posted to NetSuite as a field value.
2. SERVER-SOURCED. The label comes from the same server-executed option source
   the editable slots use — never from the model. A model-supplied label could
   name one subsidiary while the id posts to another, which is worse than
   showing the bare id.
"""

from __future__ import annotations

import json

import pytest

from app.services.chat import reference_field_labels as svc

EXT = "ext__" + "a" * 32 + "__ns_createRecord"


def _suiteql(rows):
    return json.dumps({"data": rows})


_SUBS = [
    {"id": 1, "name": "Framework Computer, Inc.", "iselimination": "F", "isinactive": "F"},
    {"id": 5, "name": "Framework Computer UK Ltd", "iselimination": "F", "isinactive": "F"},
]


async def _labels(monkeypatch, fields, *, raises=False):
    async def fake_exec(**kwargs):
        if raises:
            raise RuntimeError("MCP down")
        return _suiteql(_SUBS)

    monkeypatch.setattr("app.services.chat.slot_option_sources.execute_tool_call", fake_exec)
    svc.clear_label_cache()
    return await svc.resolve_reference_labels(
        fields,
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )


@pytest.mark.asyncio
async def test_reference_object_gets_a_human_label(monkeypatch):
    out = await _labels(monkeypatch, {"companyName": "Acme", "subsidiary": {"id": "5"}})
    assert out == {"subsidiary": "Framework Computer UK Ltd (ID 5)"}


@pytest.mark.asyncio
async def test_bare_scalar_id_is_labelled_too(monkeypatch):
    """The model sometimes sends `subsidiary: "5"` rather than a reference
    object; both reach the card, so both need labelling."""
    out = await _labels(monkeypatch, {"subsidiary": "5"})
    assert out == {"subsidiary": "Framework Computer UK Ltd (ID 5)"}


@pytest.mark.asyncio
async def test_unknown_id_is_left_unlabelled(monkeypatch):
    """An id absent from the server's own list is NOT guessed at. Showing no
    label is honest; inventing one would state a fact we cannot support."""
    out = await _labels(monkeypatch, {"subsidiary": {"id": "999"}})
    assert out == {}


@pytest.mark.asyncio
async def test_field_with_no_option_source_is_skipped(monkeypatch):
    """Only fields with a server-side option source can be labelled. A plain
    text field must never trigger a lookup."""
    out = await _labels(monkeypatch, {"companyName": "Acme", "email": "a@b.c"})
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_failure_yields_no_labels_and_does_not_raise(monkeypatch):
    """The card must still render if the lookup dies — a missing label is a
    cosmetic loss, a failed card is a blocked write."""
    out = await _labels(monkeypatch, {"subsidiary": {"id": "5"}}, raises=True)
    assert out == {}


@pytest.mark.asyncio
async def test_labels_never_mutate_the_payload(monkeypatch):
    """The whole safety property: `proposed_fields` feeds the card, but a
    label leaking into the written payload would post a display string to
    NetSuite as a field value."""
    fields = {"companyName": "Acme", "subsidiary": {"id": "5"}}
    before = json.dumps(fields, sort_keys=True)
    await _labels(monkeypatch, fields)
    assert json.dumps(fields, sort_keys=True) == before


@pytest.mark.asyncio
async def test_empty_fields_makes_no_fetch(monkeypatch):
    calls = {"n": 0}

    async def counting(**kwargs):
        calls["n"] += 1
        return _suiteql(_SUBS)

    monkeypatch.setattr("app.services.chat.slot_option_sources.execute_tool_call", counting)
    svc.clear_label_cache()
    out = await svc.resolve_reference_labels(
        {}, mutation_tool_name=EXT, tenant_id="t", actor_id="a", correlation_id="c", db=None, session_id="s"
    )
    assert out == {} and calls["n"] == 0


@pytest.mark.asyncio
async def test_options_are_cached_across_calls(monkeypatch):
    """Card rendering sits on the SSE path — one live SuiteQL per card is a
    latency cost worth paying once, not on every write."""
    calls = {"n": 0}

    async def counting(**kwargs):
        calls["n"] += 1
        return _suiteql(_SUBS)

    monkeypatch.setattr("app.services.chat.slot_option_sources.execute_tool_call", counting)
    svc.clear_label_cache()
    kw = dict(mutation_tool_name=EXT, tenant_id="t", actor_id="a", correlation_id="c", db=None, session_id="s")
    a = await svc.resolve_reference_labels({"subsidiary": "1"}, **kw)
    b = await svc.resolve_reference_labels({"subsidiary": "5"}, **kw)
    assert a == {"subsidiary": "Framework Computer, Inc. (ID 1)"}
    assert b == {"subsidiary": "Framework Computer UK Ltd (ID 5)"}
    assert calls["n"] == 1, "second card must reuse the cached option list"
