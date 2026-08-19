import json

import pytest

from app.services.chat import posting_invariants as pi
from app.services.chat.write_payload import NormalizedPayload

EXT = "ext__" + "a" * 32 + "__ns_createRecord"
KW = dict(mutation_tool_name=EXT, tenant_id=None, actor_id=None, correlation_id="c", db=None, session_id="s")


@pytest.mark.asyncio
async def test_unbalanced_journal_entry_is_rejected(monkeypatch):
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(
            fields={"trandate": "2026-08-19"},
            lines=[{"debit": 100}, {"credit": 60}],
        ),
        record_type="journalEntry",
        **KW,
    )
    assert any("balance" in e.lower() for e in errors)


@pytest.mark.asyncio
async def test_balanced_journal_entry_passes(monkeypatch):
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 100}, {"credit": 100}]),
        record_type="journalEntry",
        **KW,
    )
    assert errors == []


@pytest.mark.asyncio
async def test_closed_period_is_rejected(monkeypatch):
    async def closed(**kwargs):
        return json.dumps({"items": [{"periodname": "Jul 2026", "closed": True}]})

    monkeypatch.setattr(pi, "execute_tool_call", closed)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={"trandate": "2026-07-15"}, lines=[]),
        record_type="invoice",
        **KW,
    )
    assert any("period" in e.lower() for e in errors)


@pytest.mark.asyncio
async def test_non_transaction_record_skips_invariants(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("should not query periods for a customer")

    monkeypatch.setattr(pi, "execute_tool_call", boom)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={"companyname": "x"}, lines=[]),
        record_type="customer",
        **KW,
    )
    assert errors == []


@pytest.mark.asyncio
async def test_malformed_trandate_never_reaches_the_query(monkeypatch):
    # trandate is part of the payload under validation — i.e. attacker/LLM
    # controlled. A value that isn't a clean YYYY-MM-DD must never be
    # interpolated into the SuiteQL string, so no query should be sent at all.
    #
    # A raise-on-call callback would NOT prove this: _check_period_open wraps
    # the whole fetch in `except Exception`, so an AssertionError raised from
    # inside the call is swallowed the same as a real MCP failure and the test
    # would pass either way. Track calls explicitly instead.
    calls = []

    async def tracking(**kwargs):
        calls.append(kwargs)
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", tracking)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={"trandate": "2026-08-19') OR ('1'='1"}, lines=[]),
        record_type="invoice",
        **KW,
    )
    assert calls == []
    assert errors == []
