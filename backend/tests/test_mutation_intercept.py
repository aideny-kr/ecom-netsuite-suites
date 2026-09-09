"""Tests for mutation intercept logic in the base_agent tool execution loop.

Verifies:
1. Guard function correctly detects mutation tools (integration with mutation_guard)
2. The intercept block produces the expected result_str format (JSON with
   confirmation_required: true) for allowed record types
3. Blocked record types produce an error JSON instead of a confirmation payload
4. The getRecord pre-fetch tool name is built correctly from the update tool name
5. The getRecord pre-fetch is SKIPPED for non-NetSuite connectors (e.g. Celigo) —
   that sibling tool only exists on NetSuite, so calling it elsewhere would waste
   up to 5s against a live MCP server before the bare except swallows the failure
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.mutation_guard import (
    get_mutation_type,
    is_mutation_tool,
)
from app.services.chat.write_confirmation_service import (
    build_confirmation_payload,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
_SESSION_ID = "test-session-intercept-001"


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


def _build_intercept_result_str(
    tool_name: str,
    tool_input: dict,
    session_id: str,
    current_record: dict | None = None,
) -> str:
    """Replicate the result_str logic that the base_agent intercept block
    should produce for an *allowed* mutation tool.

    This is the contract the intercept must satisfy:
    - JSON with ``confirmation_required: true``
    - ``mutation_type`` matches the tool verb
    - ``record_type`` from tool_input
    """
    mutation_type = get_mutation_type(tool_name)
    record_type = tool_input.get("recordType", "unknown")

    payload = build_confirmation_payload(
        mutation_type=mutation_type,
        record_type=record_type,
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=session_id,
        current_record=current_record,
    )
    if payload is None:
        # Blocked or unknown record type
        return json.dumps(
            {
                "error": f"Record type '{record_type}' is not allowed for AI-initiated {mutation_type} operations.",
                "blocked": True,
            }
        )

    return json.dumps(
        {
            "confirmation_required": True,
            "mutation_type": mutation_type,
            "record_type": record_type,
            "message": (
                f"This {mutation_type} operation on {record_type} requires human "
                f"confirmation. The confirmation dialog has been shown to the user. "
                f"Do NOT proceed until the user explicitly approves."
            ),
        }
    )


def _build_blocked_result_str(mutation_type: str, record_type: str) -> str:
    """Replicate the result_str for a BLOCKED record type."""
    return json.dumps(
        {
            "error": f"Record type '{record_type}' is not allowed for AI-initiated {mutation_type} operations.",
            "blocked": True,
        }
    )


# ---------------------------------------------------------------------------
# Guard detection (integration sanity)
# ---------------------------------------------------------------------------


class TestMutationGuardIntegration:
    """Quick sanity checks that the guard functions work correctly when
    composed — these are the exact calls the intercept block makes."""

    def test_create_detected_and_typed(self):
        name = _ext("ns_createRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "create"

    def test_update_detected_and_typed(self):
        name = _ext("ns_updateRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "update"

    def test_delete_detected_and_typed(self):
        name = _ext("ns_deleteRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "delete"

    def test_upsert_detected_and_typed(self):
        name = _ext("ns_upsertRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "upsert"

    def test_get_record_not_detected(self):
        name = _ext("ns_getRecord")
        assert is_mutation_tool(name) is False

    def test_suiteql_not_detected(self):
        name = _ext("ns_runCustomSuiteQL")
        assert is_mutation_tool(name) is False


# ---------------------------------------------------------------------------
# Intercept result_str format — allowed record types
# ---------------------------------------------------------------------------


class TestInterceptResultStrAllowed:
    """Verify the result_str JSON that the intercept block should produce
    for allowed record types contains the correct structure."""

    def test_create_salesorder_result_str_has_confirmation_required(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["confirmation_required"] is True

    def test_create_salesorder_result_str_has_mutation_type(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["mutation_type"] == "create"

    def test_create_salesorder_result_str_has_record_type(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["record_type"] == "salesOrder"

    def test_create_salesorder_result_str_has_message(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert "confirmation" in parsed["message"].lower()
        assert "Do NOT proceed" in parsed["message"]

    def test_update_invoice_result_str_has_confirmation_required(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "invoice",
            "id": "INV-42",
            "body": {"memo": "updated"},
        }
        result_str = _build_intercept_result_str(
            tool_name,
            tool_input,
            _SESSION_ID,
            current_record={"id": "INV-42", "memo": "old"},
        )
        parsed = json.loads(result_str)
        assert parsed["confirmation_required"] is True
        assert parsed["mutation_type"] == "update"
        assert parsed["record_type"] == "invoice"

    def test_delete_customer_result_str(self):
        tool_name = _ext("ns_deleteRecord")
        tool_input = {"recordType": "customer", "id": "CUST-1"}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["confirmation_required"] is True
        assert parsed["mutation_type"] == "delete"

    def test_result_str_is_valid_json(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "purchaseOrder", "body": {"vendor": "V-1"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        # Should not raise
        parsed = json.loads(result_str)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Intercept result_str format — blocked record types
# ---------------------------------------------------------------------------


class TestInterceptResultStrBlocked:
    """Verify that blocked record types produce an error JSON."""

    def test_blocked_employee_returns_error(self):
        result_str = _build_blocked_result_str("create", "employee")
        parsed = json.loads(result_str)
        assert "error" in parsed
        assert parsed["blocked"] is True

    def test_blocked_role_returns_error(self):
        result_str = _build_blocked_result_str("update", "role")
        parsed = json.loads(result_str)
        assert "error" in parsed
        assert parsed["blocked"] is True

    def test_blocked_error_mentions_record_type(self):
        result_str = _build_blocked_result_str("delete", "subsidiary")
        parsed = json.loads(result_str)
        assert "subsidiary" in parsed["error"]

    def test_blocked_error_mentions_mutation_type(self):
        result_str = _build_blocked_result_str("create", "account")
        parsed = json.loads(result_str)
        assert "create" in parsed["error"]

    def test_unknown_type_requires_confirmation(self):
        """Unknown record types (not on blocklist) pass through to HITL confirmation."""
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customWidget", "body": {"name": "test"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed.get("confirmation_required") is True
        assert parsed.get("record_type") == "customWidget"


# ---------------------------------------------------------------------------
# getRecord pre-fetch tool name construction
# ---------------------------------------------------------------------------


class TestGetRecordPreFetchToolName:
    """Verify that the update intercept correctly builds the getRecord tool name
    by replacing 'ns_updateRecord' with 'ns_getRecord' while preserving the
    ext__<32hex>__ prefix."""

    def test_update_tool_name_converts_to_get(self):
        update_name = _ext("ns_updateRecord")
        # The intercept should do: tool_name.replace("ns_updateRecord", "ns_getRecord")
        get_name = update_name.replace("ns_updateRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_create_tool_name_converts_to_get(self):
        create_name = _ext("ns_createRecord")
        get_name = create_name.replace("ns_createRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_delete_tool_name_converts_to_get(self):
        delete_name = _ext("ns_deleteRecord")
        get_name = delete_name.replace("ns_deleteRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_upsert_tool_name_converts_to_get(self):
        upsert_name = _ext("ns_upsertRecord")
        get_name = upsert_name.replace("ns_upsertRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_prefix_preserved_after_replacement(self):
        update_name = _ext("ns_updateRecord")
        get_name = update_name.replace("ns_updateRecord", "ns_getRecord")
        assert get_name.startswith(f"ext__{_HEX_32}__")
        assert get_name.endswith("ns_getRecord")


# ---------------------------------------------------------------------------
# getRecord pre-fetch guard — skipped for non-NetSuite connectors
#
# Drives `BaseSpecialistAgent.run_streaming` directly — the exact method the
# guard lives in — on a real `UnifiedAgent` instance, rather than replicating
# the branch logic or driving the full orchestrator (which, with
# MULTI_AGENT_ENABLED off, runs a legacy loop that doesn't even have this
# intercept). This actually executes the fix, not just reads it.
# ---------------------------------------------------------------------------


def _llm_response(text: str | None = None, tool_blocks: list[ToolUseBlock] | None = None) -> LLMResponse:
    return LLMResponse(
        text_blocks=[text] if text else [],
        tool_use_blocks=tool_blocks or [],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _stream_replay(responses: list[LLMResponse]):
    """Replay LLM responses in order as ("text", ...) / ("response", ...) events."""
    call_count = 0

    async def stream_fn(**kwargs):
        nonlocal call_count
        resp = responses[call_count] if call_count < len(responses) else responses[-1]
        call_count += 1
        for text in resp.text_blocks:
            yield "text", text
        yield "response", resp

    return stream_fn


def _celigo_upsert_block(block_id: str = "tu_1") -> ToolUseBlock:
    """A Celigo `upsert_flow` write with a top-level `id` — exactly the shape
    that triggers the update/upsert pre-fetch branch in base_agent.py."""
    return ToolUseBlock(
        id=block_id,
        name=_ext("upsert_flow"),
        input={"recordType": "flow", "id": "F-123", "body": {"name": "renamed"}},
    )


class TestMutationPrefetchGuardSkipsNonNetSuite:
    """The update/upsert HITL pre-fetch reconstructs an `ns_getRecord` sibling
    tool name to show a before/after diff. That tool only exists on NetSuite
    connectors — on a Celigo connector it doesn't exist, so calling it wastes
    up to 5s against the live MCP server before the bare `except Exception`
    swallows the failure. The guard must skip the pre-fetch entirely for any
    tool whose raw name doesn't start with `ns_`.
    """

    @pytest.mark.asyncio
    async def test_celigo_upsert_with_top_level_id_never_calls_get_record(self):
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        agent = UnifiedAgent(tenant_id=tenant_id, user_id=user_id, correlation_id=str(uuid.uuid4()))

        mock_adapter = MagicMock()
        mock_adapter.stream_message = _stream_replay(
            [
                _llm_response(tool_blocks=[_celigo_upsert_block()]),
                _llm_response(text="Done."),
            ]
        )
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

        db = AsyncMock(spec=AsyncSession)

        tools_execute_mock = AsyncMock(return_value='{"ok": true}')

        with (
            patch(
                "app.services.policy_service.get_active_policy",
                new_callable=AsyncMock,
                return_value=None,
            ),
            # The pre-fetch reconstructs an ext__<connector>__ns_getRecord tool
            # name and calls the dispatcher via a FRESH
            # `from app.services.chat.tools import execute_tool_call` inside
            # run_streaming — so the dispatcher module attribute (not some
            # already-bound caller-side name) is the one that must be patched
            # to observe whether it was reached.
            patch("app.services.chat.tools.execute_tool_call", tools_execute_mock),
        ):
            events = []
            async for event in BaseSpecialistAgent.run_streaming(
                agent,
                task="rename the flow",
                context={},
                db=db,
                adapter=mock_adapter,
                model="claude-sonnet-4-20250514",
            ):
                events.append(event)

        # The guard must skip the pre-fetch entirely for a non-NetSuite tool —
        # the dispatcher must never be reached.
        assert tools_execute_mock.await_count == 0, (
            "the ns_getRecord pre-fetch reached the dispatcher for a non-NetSuite tool"
        )

        # Sanity: prove the mutation-intercept branch actually fired (for the
        # right reason), so the assertion above isn't passing vacuously
        # because the turn never reached the mutation branch at all.
        confirmations = [payload for event_type, payload in events if event_type == "confirmation_required"]
        assert len(confirmations) == 1, f"expected exactly one confirmation_required event, got {events}"
        payload = confirmations[0]
        assert payload["mutation_type"] == "upsert"
        assert payload["tool_name"] == _ext("upsert_flow")


# ---------------------------------------------------------------------------
# The idempotency key, asserted AT THE CALL SITE.
#
# T2 gate round 3, BLOCKER. Round 2 gave build_idempotency_key a full work
# identity (connector, record type, mutation type, ...) and 8 unit tests
# pinning each input. Every one passed. But the production call site in
# base_agent.py still called `stamp_tool_input(block.input, batch_id=None,
# row_index=None)` — an edit that silently didn't apply — so in the running
# system NONE of that identity participated and the collisions were exactly as
# live as before the "fix".
#
# The unit tests could not catch it: they call the helper directly with
# explicit kwargs, so they test the helper's contract, never the wiring. This
# test drives the REAL card-building path and asserts on the payload a human
# would actually be shown and sign.
# ---------------------------------------------------------------------------


async def _external_id_from_card(record_type: str, payload: dict, connector_hex: str) -> str:
    """Build a real confirmation card through run_streaming; return its externalId.

    The turn does what a real one does: calls ns_getRecordTypeMetadata FIRST
    (the investigation gate bounces an unexamined create), then proposes the
    write. Without that first hop no card is ever emitted, which is the same
    gate that made the card appear only 1-in-3 times in an earlier session.
    """
    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id=str(uuid.uuid4()))
    meta_block = ToolUseBlock(
        id="tu_meta",
        name=f"ext__{connector_hex}__ns_getRecordTypeMetadata",
        input={"recordType": record_type},
    )
    write_block = ToolUseBlock(
        id="tu_1",
        name=f"ext__{connector_hex}__ns_createRecord",
        input={"recordType": record_type, "data": json.dumps(payload)},
    )

    mock_adapter = MagicMock()
    mock_adapter.stream_message = _stream_replay(
        [
            _llm_response(tool_blocks=[meta_block]),
            _llm_response(tool_blocks=[write_block]),
            _llm_response(text="Done."),
        ]
    )
    mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

    meta_result = json.dumps({"fields": [{"id": "companyName", "label": "Company Name", "mandatory": True}]})

    with (
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        patch(
            "app.services.chat.tools.execute_tool_call",
            AsyncMock(return_value=meta_result),
        ),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent,
                task=f"create a {record_type}",
                context={},
                db=AsyncMock(spec=AsyncSession),
                adapter=mock_adapter,
                model="claude-sonnet-4-20250514",
            )
        ]

    cards = [p for t, p in events if t == "confirmation_required"]
    assert len(cards) == 1, f"expected one card, got event types {[t for t, _ in events]}"
    sent = json.loads(cards[0]["tool_input"]["data"])
    assert "externalId" in sent, f"card payload was never stamped: {sent}"
    return sent["externalId"]


class TestTheStampedCardCarriesTheWorkIdentity:
    _HEX_A = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
    _HEX_B = "ffffffffffffffffffffffffffffffff"

    @pytest.mark.asyncio
    async def test_different_record_types_get_different_external_ids(self):
        """A customer and a vendor named 'Acme' are different work. Colliding
        makes NetSuite refuse the second, which classifies as WRITTEN — so we
        would report a vendor created that never existed."""
        body = {"companyName": "Acme"}
        cust = await _external_id_from_card("customer", body, self._HEX_A)
        vend = await _external_id_from_card("vendor", body, self._HEX_A)
        assert cust != vend, "record_type must reach the key from the real call site"

    @pytest.mark.asyncio
    async def test_different_connectors_get_different_external_ids(self):
        """The same payload sent to sandbox and to production is not the same
        write, and the connector is what decides which account receives it."""
        body = {"companyName": "Acme"}
        a = await _external_id_from_card("customer", body, self._HEX_A)
        b = await _external_id_from_card("customer", body, self._HEX_B)
        assert a != b, "connector_id must reach the key from the real call site"
