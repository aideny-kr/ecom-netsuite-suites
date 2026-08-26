import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_unreadable_debit_and_credit_does_not_report_balanced(monkeypatch):
    # Coercing an unreadable amount to Decimal(0) would let a line whose debit
    # AND credit are both garbage compare 0 == 0 and report itself balanced —
    # a silent pass in the one check that exists to protect the ledger.
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(
            fields={},
            lines=[{"debit": "garbage", "credit": "also-garbage"}],
        ),
        record_type="journalEntry",
        **KW,
    )
    assert errors != []
    assert not any("does not balance" in e.lower() for e in errors)
    assert any("unreadable" in e.lower() for e in errors)


@pytest.mark.asyncio
async def test_one_unreadable_amount_among_balanced_lines_is_rejected(monkeypatch):
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(
            fields={},
            lines=[{"debit": 100, "credit": 100}, {"debit": "N/A"}],
        ),
        record_type="journalEntry",
        **KW,
    )
    assert any("unreadable" in e.lower() for e in errors)


@pytest.mark.asyncio
async def test_absent_and_empty_amounts_are_still_treated_as_zero(monkeypatch):
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(
            fields={},
            lines=[{"debit": 100}, {"credit": 100, "debit": ""}, {}],
        ),
        record_type="journalEntry",
        **KW,
    )
    assert errors == []


# ---------------------------------------------------------------------------
# Finding A — the closed-period check has never fired: ns_runCustomSuiteQL's
# real parameter contract is "sqlQuery", not "query" (netsuite_client.py:124,
# mcp_client_service.py:185, query_pattern_service.py:60 all confirm it).
# Every test above monkeypatches ``pi.execute_tool_call`` directly, which is
# exactly why none of them can see this bug — they never observe the
# tool_input dict that would actually reach the wire. These tests instead
# drive check_posting_invariants through the REAL execute_tool_call and stub
# only the genuine network boundary (streamablehttp_client/ClientSession),
# the same pattern test_mcp_external_client.py's _mock_mcp() uses.
# ---------------------------------------------------------------------------

_CONNECTOR_ID = uuid.UUID("a" * 32)


@dataclass
class _FakeConnector:
    id: uuid.UUID = _CONNECTOR_ID
    server_url: str = "https://example.com/mcp/v1"
    auth_type: str = "none"
    encrypted_credentials: str | None = None
    is_enabled: bool = True
    # `provider` is NOT optional on the real model (MCPConnector.provider is
    # nullable=False) and `execute_tool_call` dispatches on it — Celigo
    # connectors take a different path than NetSuite ones. Omitting it here
    # made the stub diverge from the model it stands in for, so these tests
    # died with AttributeError the moment the dispatcher started reading it.
    provider: str = "netsuite_mcp"


def _mock_mcp_wire(mock_session):
    """Stub only the network boundary — mirrors test_mcp_external_client.py."""

    @asynccontextmanager
    async def fake_streamablehttp_client(**kwargs):
        yield MagicMock(), MagicMock(), MagicMock()

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    transport_patch = patch(
        "app.services.mcp_client_service.streamablehttp_client",
        side_effect=fake_streamablehttp_client,
    )
    session_patch = patch(
        "app.services.mcp_client_service.ClientSession",
        return_value=mock_session_ctx,
    )
    connector_patch = patch(
        "app.services.mcp_connector_service.get_mcp_connector",
        new_callable=AsyncMock,
        return_value=_FakeConnector(),
    )
    return transport_patch, session_patch, connector_patch


@pytest.mark.asyncio
async def test_period_lookup_sends_sqlquery_not_query():
    """The wire-boundary proof: whatever key posting_invariants builds its
    tool_input with must be the one that actually reaches session.call_tool.
    A test that only asserts on check_posting_invariants's RETURN VALUE
    cannot catch a wrong parameter name (the tool call still "succeeds" with
    an empty/error response either way) — this asserts on the outbound
    tool_params dict itself."""
    block = MagicMock()
    block.text = json.dumps({"data": []})
    mock_result = MagicMock(isError=False, content=[block])
    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)

    tp, sp, cp = _mock_mcp_wire(mock_session)
    with tp, sp, cp:
        errors = await pi.check_posting_invariants(
            payload=NormalizedPayload(fields={"trandate": "2026-08-19"}, lines=[]),
            record_type="invoice",
            **KW,
        )

    assert errors == []
    mock_session.call_tool.assert_awaited_once()
    tool_name_sent, tool_params = mock_session.call_tool.await_args.args
    assert tool_name_sent == "ns_runCustomSuiteQL"
    assert "sqlQuery" in tool_params, f"expected 'sqlQuery' key in outbound params, got {tool_params!r}"
    assert "query" not in tool_params, f"'query' is not the real parameter — got {tool_params!r}"


@pytest.mark.asyncio
async def test_closed_period_is_detected_through_the_real_dispatcher():
    """End-to-end proof (stronger than the key-name assertion alone): with
    the wire boundary returning the documented NetSuite MCP response shape
    ({method, description, queryExecuted, resultCount, data: [...]}), a
    closed period must actually be detected through the REAL
    execute_tool_call -> mcp_client_service -> ClientSession chain — not
    just when execute_tool_call is monkeypatched away.

    ``call_tool`` here actually INSPECTS the params it receives (a plain
    fixed-return AsyncMock would happily "succeed" no matter which key was
    sent, which would make this test pass even against the pre-fix 'query'
    bug and defeat its purpose) — it only returns the closed-period row when
    'sqlQuery' is the key, mirroring how a real NetSuite MCP tool rejects an
    unrecognized/missing required parameter.
    """
    ok_block = MagicMock()
    ok_block.text = json.dumps(
        {
            "method": "ns_runCustomSuiteQL",
            "description": "posting-invariant period lookup",
            "queryExecuted": "SELECT ...",
            "resultCount": 1,
            "data": [{"periodname": "Jul 2026", "closed": "T"}],
        }
    )
    ok_result = MagicMock(isError=False, content=[ok_block])
    err_result = MagicMock(isError=True, content="sqlQuery is required")

    async def call_tool(tool_name, tool_params):
        if "sqlQuery" in tool_params:
            return ok_result
        return err_result

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = call_tool

    tp, sp, cp = _mock_mcp_wire(mock_session)
    with tp, sp, cp:
        errors = await pi.check_posting_invariants(
            payload=NormalizedPayload(fields={"trandate": "2026-07-15"}, lines=[]),
            record_type="invoice",
            **KW,
        )

    assert any("closed" in e.lower() and "jul 2026" in e.lower() for e in errors), errors


@pytest.mark.asyncio
async def test_error_shaped_response_logs_a_warning(caplog):
    """A response the tool considers an error (the realistic shape a
    param-name mismatch produces) must not vanish without a trace — today
    (pre-fix) it returns [] with ZERO log records, indistinguishable from a
    genuinely clean lookup. Regression guard against this exact silent-[]
    class recurring via a disabled connector, expired OAuth token, or MCP
    timeout — none of which involve the string 'query' at all."""

    async def error_shaped(**kwargs):
        return json.dumps({"error": "ns_runCustomSuiteQL: sqlQuery is required"})

    import logging

    with patch.object(pi, "execute_tool_call", error_shaped):
        with caplog.at_level(logging.WARNING, logger="app.services.chat.posting_invariants"):
            errors = await pi.check_posting_invariants(
                payload=NormalizedPayload(fields={"trandate": "2026-07-15"}, lines=[]),
                record_type="invoice",
                **KW,
            )

    assert errors == []
    assert any("period lookup" in r.message.lower() for r in caplog.records), (
        f"expected a warning naming the failed period lookup, got: {[r.message for r in caplog.records]}"
    )
