import json
import logging

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


# ── Boolean coercion of NetSuite's required-marker (mandatory/T/F) ──────────
#
# NetSuite has never been observed live (MCP token expired) — the parser must
# be correct under every plausible shape rather than betting on one. See
# backend/tests/fixtures/netsuite_metadata/README.md.

_TRUTHY_SPELLINGS = [True, "T", "TRUE", "true", "YES", "1"]
_FALSY_SPELLINGS = [False, None, "", "F", "FALSE", "no", "0"]


@pytest.mark.parametrize("value", _TRUTHY_SPELLINGS)
def test_truthy_required_marker_spellings(value):
    field = svc._parse_field({"name": "subsidiary", "mandatory": value})
    assert field.required is True


@pytest.mark.parametrize("value", _FALSY_SPELLINGS)
def test_falsy_required_marker_spellings(value):
    field = svc._parse_field({"name": "subsidiary", "mandatory": value})
    assert field.required is False


def test_absent_required_marker_is_not_required():
    """No marker key at all — not the same as a falsy value, but resolves the same way."""
    field = svc._parse_field({"name": "subsidiary"})
    assert field.required is False


@pytest.mark.parametrize("key", ["mandatory", "ismandatory", "required", "isrequired"])
def test_each_accepted_key_name_is_recognised(key):
    assert svc._parse_field({"name": "subsidiary", key: "T"}).required is True
    assert svc._parse_field({"name": "subsidiary", key: "F"}).required is False


def test_f_string_is_not_python_truthy_bool():
    """Regression guard: bool("F") is True in Python — must never be used bare."""
    assert svc._parse_field({"name": "subsidiary", "mandatory": "F"}).required is False


@pytest.mark.asyncio
async def test_mixed_keys_resolve_independently_in_same_response(monkeypatch):
    """{'mandatory': 'F'} and {'ismandatory': 'T'} in the same response must not
    contaminate each other — each field's marker is read from its own dict."""

    async def fake_exec(**kwargs):
        return json.dumps(
            {
                "fields": [
                    {"name": "companyname", "label": "Company Name", "mandatory": "F", "type": "text"},
                    {"name": "subsidiary", "label": "Primary Subsidiary", "ismandatory": "T", "type": "select"},
                ]
            }
        )

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
    assert meta.required_field_names() == ["subsidiary"]


@pytest.mark.asyncio
async def test_unrecognised_shape_warns_loudly_but_still_returns_metadata(monkeypatch, caplog):
    """No field in the response carries any recognised required-marker key.

    Today this degrades silently into 'nothing is required', which reads
    exactly like a legitimately permissive record type. It must instead be
    LOUD (a warning naming the record type + observed keys) — but NOT fatal,
    because a genuinely permissive record type is possible.
    """

    async def fake_exec(**kwargs):
        return json.dumps({"fields": [{"name": "companyname", "label": "Company Name", "sometotallyunknownkey": True}]})

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    with caplog.at_level(logging.WARNING, logger="app.services.chat.record_metadata_service"):
        meta = await svc.get_record_metadata(
            record_type="customer",
            mutation_tool_name=EXT,
            tenant_id=None,
            actor_id=None,
            correlation_id="c",
            db=None,
            session_id="s",
        )

    # Loud, not fatal: metadata is still usable.
    assert meta is not None
    assert meta.fields[0].name == "companyname"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "customer" in warnings[0].message
    assert "sometotallyunknownkey" in warnings[0].message


@pytest.mark.asyncio
async def test_no_warning_when_at_least_one_field_carries_a_marker(monkeypatch, caplog):
    """The loud-shape-mismatch warning must not fire just because SOME fields
    lack a marker key — only when NONE of them carry one."""

    async def fake_exec(**kwargs):
        return json.dumps(
            {
                "fields": [
                    {"name": "companyname", "label": "Company Name"},  # no marker key at all
                    {"name": "subsidiary", "label": "Primary Subsidiary", "mandatory": True},
                ]
            }
        )

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    with caplog.at_level(logging.WARNING, logger="app.services.chat.record_metadata_service"):
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
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []
