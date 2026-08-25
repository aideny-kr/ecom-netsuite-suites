"""Tests for the orchestrator's approve-failure bounded write-repair loop
(agentic-repair design requirement B/D) — the terminal exits (stall/budget)
and the fall-through re-entry into the agent on a rejection that still has
budget.

Terminal exits still `return` after persisting state and emitting a message,
so they're testable with the same DB-mocking pattern
test_write_confirm_orchestrator.py already uses.

The reenter path falls through into the FULL agent turn pipeline (context
assembly, RAG, entity resolution, tool inventory, knowledge profiles,
UnifiedAgent construction). A first attempt at mocking that whole pipeline
end to end — patching UnifiedAgent, get_tenant_ai_config, get_adapter, etc.
— reached far enough to trigger a REAL, unmocked Anthropic API call inside
the tenant-name-entity resolver (a SEPARATE LLM call the orchestrator makes
for context assembly, on its own adapter path this test doesn't control).
That is unacceptable in a unit test (cost, non-determinism, network
dependency, no existing precedent for it in this suite — see
test_chat_agentic.py's own comment that no such harness exists for the
unified-agent path). Instead: a deterministic TRIPWIRE. Patching
`_ensure_session_messages_loaded` (called immediately after the write_confirm
short-circuit, before any RAG/LLM work) to raise a sentinel proves execution
reached PAST the write_confirm block without needing the rest of the
pipeline to run — the sentinel is unreachable if the code still `return`ed
inside the approve branch. The downstream wiring this can't observe (that
base_agent.py's mutation intercept correctly reads whatever gets injected
onto the agent, and that UnifiedAgent.system_prompt surfaces the directive)
is independently covered by test_ask_user_and_repair_chain.py and
test_write_repair_directive_injection.py; a static source-inspection test
below pins that the orchestrator's OWN injection lines exist at the
UnifiedAgent construction site, mirroring the existing Plan Mode injection
it's modeled on (same technique test_orchestrator_metric_source_pin.py
already uses for wiring that has no safe dynamic harness).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat import ChatMessage, ChatSession

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


def _make_session(session_id):
    session = MagicMock(spec=ChatSession)
    session.id = session_id
    session.tenant_id = _TENANT_ID
    session.session_type = "chat"
    session.source_pin = None
    session.workspace_id = None
    session.agent_id = None
    session.messages = []
    session.title = "Test Session"
    return session


def _make_confirm_msg(session_id, *, repair_of=None, repair_attempt=0, status="pending"):
    from app.services.chat.write_confirmation_service import build_confirmation_payload

    tool_name = _ext("ns_createRecord")
    tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="customer",
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=str(session_id),
        repair_of=repair_of,
        repair_attempt=repair_attempt,
    )
    assert payload is not None

    msg = ChatMessage(
        tenant_id=_TENANT_ID,
        session_id=session_id,
        role="assistant",
        content="",
        structured_output={**payload.model_dump(), "status": status},
        created_at=datetime.now(timezone.utc),
    )
    msg.id = uuid.uuid4()
    return msg, tool_name, tool_input


class _FakeScalarResult:
    def __init__(self, msg):
        self._msg = msg

    def scalar_one_or_none(self):
        return self._msg


def _make_db(confirm_msg, cas_rowcount: int = 1):
    from sqlalchemy import Update

    db = MagicMock()

    async def _execute(stmt, *args, **kwargs):
        if isinstance(stmt, Update):
            return MagicMock(rowcount=cas_rowcount)
        return _FakeScalarResult(confirm_msg)

    db.execute = _execute
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# Terminal exits — budget / stall. Still `return` after persisting, so no
# agent-pipeline mocking needed.
# ---------------------------------------------------------------------------


class TestRepairBoundTerminalExits:
    @pytest.mark.asyncio
    async def test_budget_exhausted_is_terminal_and_names_the_reason(self):
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, tool_name, tool_input = _make_confirm_msg(
            session_id, repair_of=str(uuid.uuid4()), repair_attempt=2
        )
        db = _make_db(confirm_msg)
        session = _make_session(session_id)

        mock_execute_tool_call = AsyncMock(
            return_value=json.dumps({"error": "HTTP 400: Please enter value(s) for: Currency."})
        )
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
            patch(
                "app.services.chat.orchestrator._resolve_repair_chain_previous_fingerprint",
                new_callable=AsyncMock,
                return_value="a-completely-different-fingerprint",
            ),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="create a customer",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

        so = confirm_msg.structured_output
        assert so["status"] == "failed"
        assert so["repair_exit_reason"] == "budget"
        assert so["failure_fingerprint"] is not None

        message_events = [e for e in events if e.get("type") == "message"]
        assert message_events
        content = message_events[-1]["message"]["content"]
        assert "budget" in content.lower() or "used up" in content.lower()
        assert "Currency" in content

        # Terminal — must still return cleanly, exactly once.
        mock_execute_tool_call.assert_awaited_once()
        actions = [c.kwargs.get("action") for c in mock_log_event.call_args_list]
        assert "record.create.repair_exhausted" in actions

    @pytest.mark.asyncio
    async def test_stall_on_identical_fingerprint_is_terminal(self):
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, tool_name, tool_input = _make_confirm_msg(
            session_id, repair_of=str(uuid.uuid4()), repair_attempt=1
        )
        db = _make_db(confirm_msg)
        session = _make_session(session_id)

        error_text = "HTTP 400: Please enter value(s) for: Primary Subsidiary."
        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"error": error_text}))
        mock_log_event = AsyncMock(return_value=None)

        from app.services.chat.write_repair_bound import compute_failure_fingerprint

        same_fingerprint, _ = compute_failure_fingerprint(json.dumps({"error": error_text}), error_text)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
            patch(
                "app.services.chat.orchestrator._resolve_repair_chain_previous_fingerprint",
                new_callable=AsyncMock,
                return_value=same_fingerprint,
            ),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="create a customer",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

        so = confirm_msg.structured_output
        assert so["status"] == "failed"
        assert so["repair_exit_reason"] == "stall"

        message_events = [e for e in events if e.get("type") == "message"]
        assert message_events
        assert "same problem" in message_events[-1]["message"]["content"].lower()


# ---------------------------------------------------------------------------
# Reenter — fall-through into the agent. See module docstring for why this
# is a tripwire rather than a full-pipeline drive.
# ---------------------------------------------------------------------------


class TestRepairBoundReenter:
    @pytest.mark.asyncio
    async def test_reenter_does_not_return_and_re_establishes_tenant_context(self):
        """The bound's most safety-critical property, proved directly: a
        rejection with budget remaining does NOT return from the
        write_confirm short-circuit — it reaches code that only exists past
        that block (the sentinel), with the card left in a
        NON-terminal-but-failed state (no repair_exit_reason — that would
        misreport an attempt that hasn't exited yet) and tenant context
        re-established for the tenant-scoped work that follows.

        This test FAILS if the bound were removed (i.e., if approve-failure
        always returned unconditionally): the sentinel would simply never
        fire and pytest.raises would report it was never raised."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, tool_name, tool_input = _make_confirm_msg(session_id, repair_of=None, repair_attempt=0)
        db = _make_db(confirm_msg)
        session = _make_session(session_id)

        mock_execute_tool_call = AsyncMock(
            return_value=json.dumps({"error": "HTTP 400: Please enter value(s) for: Primary Subsidiary."})
        )
        mock_log_event = AsyncMock(return_value=None)
        mock_set_tenant_context = AsyncMock(return_value=None)

        class _SentinelError(RuntimeError):
            pass

        mock_ensure_loaded = AsyncMock(side_effect=_SentinelError("reached the normal agent turn pipeline"))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
            patch("app.core.database.set_tenant_context", mock_set_tenant_context),
            patch("app.services.chat.orchestrator._ensure_session_messages_loaded", mock_ensure_loaded),
        ):
            with pytest.raises(_SentinelError):
                async for _event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="create a customer named Acme",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                ):
                    pass

        so = confirm_msg.structured_output
        assert so["status"] == "failed"
        assert so["error"]
        assert so["failure_fingerprint"] is not None
        # Absence is the point: a repair_exit_reason on a card that is
        # about to re-enter would misreport an attempt that hasn't exited.
        assert "repair_exit_reason" not in so

        # Re-established AFTER the failure-path commit, in addition to the
        # existing CAS-claim re-establishment — proves the specific new
        # call, not just that tenant context was touched once somewhere.
        assert mock_set_tenant_context.await_count >= 2

    @pytest.mark.asyncio
    async def test_budget_exhausted_does_not_reach_the_sentinel(self):
        """Mirror check: a TERMINAL exit must still `return` before the
        sentinel — proves the tripwire in the test above is actually
        discriminating reenter from terminal, not just always firing."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, tool_name, tool_input = _make_confirm_msg(
            session_id, repair_of=str(uuid.uuid4()), repair_attempt=2
        )
        db = _make_db(confirm_msg)
        session = _make_session(session_id)

        mock_execute_tool_call = AsyncMock(
            return_value=json.dumps({"error": "HTTP 400: Please enter value(s) for: Currency."})
        )
        mock_log_event = AsyncMock(return_value=None)
        mock_ensure_loaded = AsyncMock(side_effect=AssertionError("must not reach the agent pipeline"))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
            patch(
                "app.services.chat.orchestrator._resolve_repair_chain_previous_fingerprint",
                new_callable=AsyncMock,
                return_value="a-completely-different-fingerprint",
            ),
            patch("app.services.chat.orchestrator._ensure_session_messages_loaded", mock_ensure_loaded),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="create a customer",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

        mock_ensure_loaded.assert_not_called()
        assert confirm_msg.structured_output["repair_exit_reason"] == "budget"
        assert any(e.get("type") == "message" for e in events)


# ---------------------------------------------------------------------------
# Static wiring pin — the downstream half (base_agent.py reading
# _write_repair_context, UnifiedAgent.system_prompt surfacing the directive)
# is covered dynamically elsewhere; this pins that the ORCHESTRATOR actually
# sets both attributes at the UnifiedAgent construction site, using the same
# source-inspection technique test_orchestrator_metric_source_pin.py uses
# for wiring with no safe dynamic harness.
# ---------------------------------------------------------------------------


class TestWriteRepairInjectionWiring:
    def test_write_repair_context_injected_at_unified_agent_construction(self):
        import inspect

        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        idx = source.index("if plan_mode_resume_directive:")
        window = source[idx : idx + 1600]
        assert "unified_agent._write_repair_directive = write_repair_directive" in window
        assert "unified_agent._write_repair_context = write_repair_context" in window
        assert "if write_repair_context is not None:" in window

    def test_fall_through_branch_has_no_return_statement_before_the_injection_site(self):
        """The single most important negative check: no `return` STATEMENT
        (as opposed to the word appearing in a comment explaining the
        design) sits between the reenter decision and the UnifiedAgent
        construction that would silently defeat the fall-through."""
        import inspect
        import re

        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        start = source.index("# ── Fall through into the agent (requirement B) ──")
        end = source.index('elif _wc_action == "reject":')
        window = source[start:end]
        code_lines = [line for line in window.splitlines() if not line.strip().startswith("#")]
        return_statement = re.compile(r"^\s*return\b")
        assert not any(return_statement.match(line) for line in code_lines), (
            "found a `return` statement between the reenter decision and the "
            "UnifiedAgent construction — this would silently defeat the fall-through"
        )
