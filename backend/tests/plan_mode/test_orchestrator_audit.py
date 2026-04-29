"""Tests for codex round 6 P2 fixes:

Bug 1 — audit log failure on plan_mode_choice resume must NOT strand the
chosen card. Either: (Option A) the CAS is reverted so the user can retry,
or (Option B) a fallback ChatDisclosureEvent is written so the choice is
still observable. We implement Option A here — the audit and the
``pending → chosen`` transition are made effectively atomic by adding a
revert step on audit failure.

Bug 2 — when persisting a clarification assistant message, the
``ChatDisclosureEvent.chat_message_id`` was being read from
``assistant_msg.id`` BEFORE the row was flushed to the DB. SQLAlchemy's
Python-side ``default=uuid.uuid4`` doesn't fire until flush, so the
disclosure row was persisted with ``chat_message_id = NULL`` and
clarification telemetry could not be linked to its message.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bug 1 — audit failure must revert the CAS so the user can retry.
# ---------------------------------------------------------------------------


class TestPlanModeChoiceAuditAtomicity:
    """Source-level invariants on the orchestrator's plan_mode_choice block.

    The block must:
      1. Wrap the ``log_event`` call for ``plan_mode.chose`` in a try/except
         so an audit-store failure cannot abort the turn silently while
         leaving the structured_output stuck in ``status='chosen'``.
      2. On exception, call a ``revert_clarification_to_pending``-named
         helper so the user can retry the resume turn (the next attempt
         will find ``status='pending'`` again and proceed normally).
      3. Re-raise (or yield an error event) so the failure is surfaced —
         the choice did not land, and the caller MUST know.
    """

    def test_audit_call_is_wrapped_in_try_except(self) -> None:
        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        chose_idx = source.find('"plan_mode.chose"')
        assert chose_idx >= 0, "expected plan_mode.chose log_event in run_chat_turn"

        # Look in a window around the audit call for a `try:` introducer
        # before the call and an `except` clause after the call. The
        # try/except can span ~50 lines because the rollback path is
        # explicit, so we widen the window generously.
        before_window = source[max(0, chose_idx - 600) : chose_idx]
        after_window = source[chose_idx : min(len(source), chose_idx + 2000)]
        assert "try:" in before_window, (
            "The plan_mode.chose log_event MUST be wrapped in try/except so "
            "an audit-store failure does not strand the chosen card "
            "(codex round 6 Bug 1)"
        )
        assert re.search(r"except\b", after_window), "Missing except clause around plan_mode.chose log_event"

    def test_revert_helper_is_imported_or_referenced(self) -> None:
        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        assert "revert_clarification_to_pending" in source, (
            "On audit failure the orchestrator MUST call "
            "revert_clarification_to_pending so the CAS can be undone "
            "and the user can retry (codex round 6 Bug 1, Option A)"
        )

    def test_plan_mode_choice_result_carries_message_id(self) -> None:
        """The revert helper needs the chat_message_id; it must be exposed
        on PlanModeChoiceResult so the orchestrator can pass it back.
        """
        from app.services.chat.plan_mode.short_circuit import PlanModeChoiceResult

        # Construct a result with the new field and confirm the type accepts it.
        result = PlanModeChoiceResult(
            chosen_option={"id": "A"},
            chosen_source="netsuite",
            system_directive="...",
            chat_message_id=uuid.uuid4(),
        )
        assert result.chat_message_id is not None


class TestRevertClarificationToPending:
    """Unit tests for the new ``revert_clarification_to_pending`` helper.

    The helper must atomically flip ``structured_output.status`` from
    ``'chosen'`` back to ``'pending'`` for a single chat_message id, only
    if the row is currently in ``'chosen'`` (so a concurrent
    ``supersede_pending_clarifications`` cannot be silently undone).
    """

    @pytest.mark.asyncio
    async def test_revert_is_idempotent_atomic_cas(self) -> None:
        from unittest.mock import AsyncMock as _AM

        from app.services.chat.plan_mode.short_circuit import (
            revert_clarification_to_pending,
        )

        db = _AM()
        update_result = MagicMock(rowcount=1)

        async def _execute(*_args, **_kwargs):
            return update_result

        db.execute = _execute
        db.commit = _AM()

        msg_id = uuid.uuid4()
        result = await revert_clarification_to_pending(
            message_id=msg_id,
            tenant_id=uuid.uuid4(),
            db=db,
        )
        # Returns True when the CAS flipped a row back; False when no-op.
        assert result is True
        db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_revert_is_no_op_when_not_chosen(self) -> None:
        from unittest.mock import AsyncMock as _AM

        from app.services.chat.plan_mode.short_circuit import (
            revert_clarification_to_pending,
        )

        db = _AM()
        update_result = MagicMock(rowcount=0)

        async def _execute(*_args, **_kwargs):
            return update_result

        db.execute = _execute
        db.commit = _AM()

        msg_id = uuid.uuid4()
        result = await revert_clarification_to_pending(
            message_id=msg_id,
            tenant_id=uuid.uuid4(),
            db=db,
        )
        assert result is False


class TestHandlePlanModeChoiceExposesMessageId:
    """``handle_plan_mode_choice`` must populate ``chat_message_id`` on the
    PlanModeChoiceResult so the orchestrator can pass it to the revert
    helper if the audit step fails.
    """

    @pytest.mark.asyncio
    async def test_success_result_has_chat_message_id(self) -> None:
        """Replicates the happy-path mock from test_short_circuit.py and
        asserts the new field is populated.
        """
        from unittest.mock import AsyncMock as _AM

        from app.services.chat.mutation_guard import generate_confirmation_token
        from app.services.chat.plan_mode.short_circuit import (
            PlanModeChoiceResult,
            handle_plan_mode_choice,
        )

        session_id = str(uuid.uuid4())
        options = [
            {
                "id": "A",
                "title": "NetSuite GL",
                "rationale": "GL",
                "source": "netsuite",
                "is_default": True,
            },
            {
                "id": "B",
                "title": "BigQuery",
                "rationale": "BQ",
                "source": "bigquery",
                "is_default": False,
            },
        ]
        payload_for_hmac = json.dumps({"options": options, "default_id": "A"}, sort_keys=True)
        token = generate_confirmation_token(session_id, payload_for_hmac, event_type="plan_mode_choice")
        so = {
            "type": "clarification",
            "status": "pending",
            "options": options,
            "default_id": "A",
            "ambiguity_summary": "summary",
            "confirmation_token": token,
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

        msg = MagicMock()
        msg.id = uuid.uuid4()
        msg.session_id = uuid.UUID(session_id)
        msg.structured_output = so

        db = _AM()
        select_result = MagicMock()
        select_result.scalar_one_or_none = MagicMock(return_value=msg)
        update_result = MagicMock(rowcount=1)
        _calls = {"n": 0}

        async def _execute(*_args, **_kwargs):
            _calls["n"] += 1
            return select_result if _calls["n"] == 1 else update_result

        db.execute = _execute
        db.add = MagicMock()
        db.commit = _AM()

        result = await handle_plan_mode_choice(
            plan_mode_choice={
                "action": "approve",
                "confirmation_id": str(msg.id),
                "option_id": "A",
            },
            session_id=session_id,
            tenant_id=uuid.uuid4(),
            db=db,
        )
        assert isinstance(result, PlanModeChoiceResult)
        assert result.chat_message_id == msg.id


# ---------------------------------------------------------------------------
# Bug 2 — assistant_msg.id is None when the disclosure_event is appended,
# because SQLAlchemy hasn't flushed the row. Persisted disclosure rows then
# have NULL chat_message_id and can't be joined back to their message.
# ---------------------------------------------------------------------------


class TestDisclosureEventLinksToFlushedMessageId:
    """Source-level invariants on the orchestrator clarification persist
    block. The ``ChatDisclosureEvent`` row must be appended to the session
    AFTER ``assistant_msg`` has been flushed, so its UUID is populated.
    """

    def test_flush_precedes_disclosure_event_construction(self) -> None:
        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)

        # Find the clarification disclosure event construction. There is a
        # single such occurrence in the orchestrator (the "clarification_pending"
        # event_type literal).
        idx = source.find('event_type="clarification_pending"')
        assert idx >= 0, "expected clarification_pending ChatDisclosureEvent in orchestrator"

        # Walk backwards to find the enclosing `if` block so we can scope
        # the search for `assistant_msg.id`. The structure is:
        #
        #   if isinstance(_persisted_output, dict) and ...:
        #       from app.models.chat_disclosure_event import ChatDisclosureEvent
        #       db.add(
        #           ChatDisclosureEvent(
        #               ...
        #               chat_message_id=assistant_msg.id,
        #               ...
        #           )
        #       )
        if_idx = source.rfind("if isinstance(_persisted_output, dict)", 0, idx)
        assert if_idx >= 0, "could not find guarding if-block"

        # Look for `db.add(assistant_msg)` BEFORE the if-block — we expect
        # it to be there, and a flush must come between it and the
        # disclosure event construction.
        add_idx = source.rfind("db.add(assistant_msg)", 0, if_idx)
        assert add_idx >= 0, "expected db.add(assistant_msg) to precede the clarification disclosure event"

        # Between db.add(assistant_msg) and the disclosure event, there
        # MUST be an explicit `await db.flush()` call so SQLAlchemy
        # populates the Python-side `id` default before we read it.
        between = source[add_idx:idx]
        assert "await db.flush()" in between, (
            "Bug 2 — `assistant_msg.id` is read into ChatDisclosureEvent "
            "without a preceding `await db.flush()`. The default uuid is "
            "only populated at flush time, so the disclosure row is "
            "persisted with chat_message_id=NULL. Add `await db.flush()` "
            "after `db.add(assistant_msg)` and before reading "
            "`assistant_msg.id`."
        )


class TestSupersedeAlreadyUsesLoadedMessageId:
    """Sanity check — the supersede telemetry path uses a row LOADED from
    the DB, so its `id` is already populated. The bug only affects rows
    that were just `db.add()`'d in the same session.
    """

    def test_supersede_uses_loaded_msg_id(self) -> None:
        from app.services.chat.plan_mode import short_circuit

        source = inspect.getsource(short_circuit.supersede_pending_clarifications)
        # The function fetches via `pending_result.scalars().all()` -> msg.id
        # is populated from the DB row, no flush dance required.
        assert "select(ChatMessage)" in source
        assert "chat_message_id=msg.id" in source
