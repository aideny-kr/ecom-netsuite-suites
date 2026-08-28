"""Unit tests for the server-side editable-slot option-source registry.

The registry is the authorization boundary: a model may HINT a field name via
`ask_user`, but every VALUE offered to the human comes from a server-executed,
code-reviewed tool call — never from the model. These tests prove that
boundary in isolation, before any wiring into base_agent.py's intercept.
"""

import json

import pytest

from app.services.chat import slot_option_sources as svc
from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

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


# ---------------------------------------------------------------------------
# resolve_ask_user_slots — the (C) server-authority boundary. The model
# contributes a NAME only; every VALUE comes from a server-executed fetch.
# ---------------------------------------------------------------------------

_METADATA = RecordMetadata(
    record_type="customer",
    fields=[
        FieldSpec(name="subsidiary", label="Primary Subsidiary", type="select"),
        FieldSpec(name="companyname", label="Company Name", type="text"),
    ],
    requirements_known=False,
)


class TestResolveAskUserSlots:
    @pytest.mark.asyncio
    async def test_registered_and_verified_name_becomes_a_slot(self, monkeypatch):
        async def fake_exec(**kwargs):
            return json.dumps({"subsidiaries": [{"id": "1", "name": "Framework Inc"}]})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["subsidiary"],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert rejected == []
        assert len(slots) == 1
        assert slots[0].name == "subsidiary"
        assert slots[0].label == "Primary Subsidiary"
        assert slots[0].allowed == [{"value": "1", "label": "Framework Inc"}]

    @pytest.mark.asyncio
    async def test_name_not_in_metadata_properties_is_rejected_no_fetch(self, monkeypatch):
        calls = {"n": 0}

        async def fake_exec(**kwargs):
            calls["n"] += 1
            return json.dumps({"subsidiaries": []})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["not_a_real_field"],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert slots == []
        assert calls["n"] == 0
        assert len(rejected) == 1
        assert rejected[0]["name"] == "not_a_real_field"

    @pytest.mark.asyncio
    async def test_name_in_metadata_but_not_registered_is_rejected_no_fetch(self, monkeypatch):
        """companyname is a real field on the record type but has no
        server-side option source — the model naming a real field is NOT
        sufficient, it must also be in the registry."""
        calls = {"n": 0}

        async def fake_exec(**kwargs):
            calls["n"] += 1
            return json.dumps({})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["companyname"],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert slots == []
        assert calls["n"] == 0
        assert rejected[0]["name"] == "companyname"

    @pytest.mark.asyncio
    async def test_zero_options_from_fetch_declares_no_slot(self, monkeypatch):
        async def fake_exec(**kwargs):
            return json.dumps({"subsidiaries": []})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["subsidiary"],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert slots == []
        assert rejected[0]["name"] == "subsidiary"

    @pytest.mark.asyncio
    async def test_metadata_none_rejects_every_hinted_name_without_fetching(self, monkeypatch):
        calls = {"n": 0}

        async def fake_exec(**kwargs):
            calls["n"] += 1
            return json.dumps({"subsidiaries": []})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["subsidiary"],
            metadata=None,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert slots == []
        assert calls["n"] == 0
        assert rejected[0]["name"] == "subsidiary"

    @pytest.mark.asyncio
    async def test_non_list_hint_yields_nothing_and_never_raises(self):
        slots, rejected = await svc.resolve_ask_user_slots(
            "subsidiary",  # a bare string, not a list — malformed model output
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert slots == []
        assert rejected == []

    @pytest.mark.asyncio
    async def test_non_string_entries_in_hint_are_rejected_not_raised(self):
        slots, rejected = await svc.resolve_ask_user_slots(
            [123, {"name": "subsidiary"}],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert slots == []
        assert len(rejected) == 2

    @pytest.mark.asyncio
    async def test_name_already_declared_is_skipped_silently_not_double_fetched(self, monkeypatch):
        """A field already carrying a server-derived slot (from missing_required)
        must not be re-resolved or duplicated — and must not appear in
        `rejected` either, since it's not actually a problem."""
        calls = {"n": 0}

        async def fake_exec(**kwargs):
            calls["n"] += 1
            return json.dumps({"subsidiaries": [{"id": "1", "name": "Framework Inc"}]})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["subsidiary"],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
            already_declared=["subsidiary"],
        )
        assert slots == []
        assert rejected == []
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_duplicate_hinted_names_resolved_once(self, monkeypatch):
        # Counts RESOLUTIONS, not raw tool calls: one resolution now costs one
        # call when SuiteQL answers and two when it falls back to the report
        # tool, so the SuiteQL shape is returned here to keep this test about
        # deduplication rather than about the number of sources.
        calls = {"n": 0}

        async def fake_exec(**kwargs):
            calls["n"] += 1
            return json.dumps({"data": [{"id": 1, "name": "Framework Inc", "iselimination": "F", "isinactive": "F"}]})

        monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
        slots, rejected = await svc.resolve_ask_user_slots(
            ["subsidiary", "subsidiary"],
            metadata=_METADATA,
            mutation_tool_name=EXT,
            tenant_id="t",
            actor_id="a",
            correlation_id="c",
            db=None,
            session_id="s",
        )
        assert len(slots) == 1
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Which subsidiaries a human may actually be offered
# ---------------------------------------------------------------------------
#
# `ns_getSubsidiaries` is built for the REPORT tool, so it returns everything a
# report can be filtered by — including constructs no record can be assigned
# to. Verified live against account 6738075 on 2026-08-25: it returned SIX
# entries while `SELECT ... FROM subsidiary` returned FIVE. The extra was
# "Framework Computer, Inc. (Consolidated)" with id -1, a roll-up that exists
# only for reporting; and among the five, "Elimination Subsidiary" carries
# iselimination = "T".
#
# Offering either as a choice hands the operator a dropdown entry that fails on
# approve. The exclusion rule mirrors required_field_registry's asymmetry, in
# the same direction: wrongly EXCLUDING a valid subsidiary blocks a legitimate
# write, while wrongly including one fails at NetSuite and recovers via the
# repair loop. So a row is dropped only when we affirmatively know it is bad —
# a null/absent flag keeps the subsidiary.


def _suiteql(rows):
    return json.dumps({"data": rows})


def _tool(subs):
    return json.dumps({"subsidiaries": subs})


async def _fetch(monkeypatch, *, suiteql=None, tool=None, suiteql_raises=False):
    async def fake_exec(**kwargs):
        name = kwargs.get("tool_name", "")
        if name.endswith("ns_runCustomSuiteQL"):
            if suiteql_raises:
                raise RuntimeError("SuiteQL unavailable for this role")
            return suiteql
        if name.endswith("ns_getSubsidiaries"):
            return tool
        return "{}"

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    return await svc.fetch_options(
        "subsidiary",
        mutation_tool_name=EXT,
        tenant_id="t",
        actor_id="a",
        correlation_id="c",
        db=None,
        session_id="s",
    )


@pytest.mark.asyncio
async def test_elimination_and_inactive_subsidiaries_are_not_offered(monkeypatch):
    options = await _fetch(
        monkeypatch,
        suiteql=_suiteql(
            [
                {"id": 1, "name": "Framework Computer, Inc.", "iselimination": "F", "isinactive": "F"},
                {"id": 3, "name": "Elimination Subsidiary", "iselimination": "T", "isinactive": "F"},
                {"id": 9, "name": "Retired Entity", "iselimination": "F", "isinactive": "T"},
            ]
        ),
    )
    assert [o["label"] for o in options] == ["Framework Computer, Inc."]


@pytest.mark.asyncio
async def test_a_missing_flag_keeps_the_subsidiary(monkeypatch):
    """Excluding on an absent flag would hide a valid subsidiary and block a
    legitimate write — the expensive direction of this trade."""
    options = await _fetch(
        monkeypatch,
        suiteql=_suiteql(
            [
                {"id": 1, "name": "No Flags At All"},
                {"id": 2, "name": "Null Flags", "iselimination": None, "isinactive": None},
            ]
        ),
    )
    assert [o["label"] for o in options] == ["No Flags At All", "Null Flags"]


@pytest.mark.asyncio
async def test_consolidated_rollup_is_never_offered(monkeypatch):
    """A negative id is NetSuite's marker for a consolidated roll-up (stated in
    ns_getSubsidiaries' own tool description). No record can be assigned to
    one, and it is absent from the subsidiary table entirely."""
    options = await _fetch(
        monkeypatch,
        suiteql=_suiteql([]),
        tool=_tool(
            [
                {"id": "1", "name": "Framework Computer, Inc."},
                {"id": "-1", "name": "Framework Computer, Inc. (Consolidated)"},
            ]
        ),
    )
    assert [o["value"] for o in options] == ["1"]


@pytest.mark.asyncio
async def test_falls_back_to_the_report_tool_when_suiteql_fails(monkeypatch):
    """SuiteQL needs search permissions the purpose-built tool does not. Losing
    the dropdown entirely on a role that cannot run SuiteQL would be a worse
    regression than offering a slightly coarser list."""
    options = await _fetch(
        monkeypatch,
        suiteql_raises=True,
        tool=_tool([{"id": "1", "name": "Framework Computer, Inc."}]),
    )
    assert options == [{"value": "1", "label": "Framework Computer, Inc."}]


@pytest.mark.asyncio
async def test_suiteql_error_payload_also_falls_back(monkeypatch):
    options = await _fetch(
        monkeypatch,
        suiteql=json.dumps({"error": "Invalid search query"}),
        tool=_tool([{"id": "2", "name": "Framework Computer B.V."}]),
    )
    assert options == [{"value": "2", "label": "Framework Computer B.V."}]


@pytest.mark.asyncio
async def test_both_sources_empty_yields_no_slot(monkeypatch):
    """Callers must treat an empty result as "decline to declare a slot" — an
    empty dropdown is a dead end at approve time."""
    assert await _fetch(monkeypatch, suiteql=_suiteql([]), tool=_tool([])) == []


@pytest.mark.asyncio
async def test_suiteql_is_preferred_and_short_circuits_the_report_tool(monkeypatch):
    called = []

    async def fake_exec(**kwargs):
        name = kwargs.get("tool_name", "")
        called.append(name.split("__")[-1])
        if name.endswith("ns_runCustomSuiteQL"):
            return _suiteql([{"id": 1, "name": "Framework Computer, Inc.", "iselimination": "F", "isinactive": "F"}])
        return _tool([{"id": "1", "name": "should not be reached"}])

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
    assert called == ["ns_runCustomSuiteQL"]
    assert options == [{"value": "1", "label": "Framework Computer, Inc."}]
