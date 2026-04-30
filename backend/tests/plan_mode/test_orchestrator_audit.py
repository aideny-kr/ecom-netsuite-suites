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

import asyncio
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
    """Unit tests for the ``revert_clarification_to_pending`` helper.

    The helper must flip ``structured_output.status`` from ``'chosen'``
    back to ``'pending'`` for a single chat_message id, only if the row
    is currently in ``'chosen'`` (so a concurrent
    ``supersede_pending_clarifications`` cannot be silently undone).

    Round 7 Bug 3: the implementation switched from ``UPDATE ... jsonb_set``
    to a Python round-trip via ``db.get`` because ``ChatMessage.structured_output``
    is declared as JSON not JSONB; jsonb_set has no JSON signature in PG.
    """

    @pytest.mark.asyncio
    async def test_revert_flips_status_when_chosen(self) -> None:
        from unittest.mock import AsyncMock as _AM

        from app.services.chat.plan_mode.short_circuit import (
            revert_clarification_to_pending,
        )

        msg_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        msg = MagicMock()
        msg.id = msg_id
        msg.tenant_id = tenant_id
        msg.structured_output = {"type": "clarification", "status": "chosen"}

        db = _AM()
        db.get = _AM(return_value=msg)
        db.commit = _AM()

        result = await revert_clarification_to_pending(
            message_id=msg_id,
            tenant_id=tenant_id,
            db=db,
        )
        assert result is True
        assert msg.structured_output["status"] == "pending"
        db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_revert_is_no_op_when_not_chosen(self) -> None:
        from unittest.mock import AsyncMock as _AM

        from app.services.chat.plan_mode.short_circuit import (
            revert_clarification_to_pending,
        )

        msg_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        msg = MagicMock()
        msg.id = msg_id
        msg.tenant_id = tenant_id
        # Already pending -> revert should no-op.
        msg.structured_output = {"type": "clarification", "status": "pending"}

        db = _AM()
        db.get = _AM(return_value=msg)
        db.commit = _AM()

        result = await revert_clarification_to_pending(
            message_id=msg_id,
            tenant_id=tenant_id,
            db=db,
        )
        assert result is False
        db.commit.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# Round 7 Bug 1 — broaden audit-revert to cover the resumed turn body.
#
# Round 6 only reverted on log_event failure. But the resumed turn has many
# downstream failure points (LLM stream timeout, tool error, persist commit)
# that ALSO need revert protection — otherwise the user's choice is locked
# in (status='chosen') with no answer ever produced; retry → 409.
#
# These tests drive run_chat_turn through the plan_mode_choice path with
# the audit succeeding, then force a downstream failure, and assert that
# revert_clarification_to_pending is called.
# ---------------------------------------------------------------------------


def _build_session(session_id: uuid.UUID | None = None) -> MagicMock:
    """Minimal ChatSession-shaped mock for orchestrator tests."""
    sess = MagicMock()
    sess.id = session_id or uuid.uuid4()
    sess.tenant_id = uuid.uuid4()
    sess.session_type = "chat"
    sess.source_pin = None
    sess.workspace_id = None
    sess.agent_id = None
    sess.messages = []
    sess.title = None
    return sess


class TestResumeTurnFailureReverts:
    """Round 7 Bug 1 — any failure AFTER handle_plan_mode_choice succeeds
    must trigger revert_clarification_to_pending so the user can retry.
    """

    @pytest.mark.asyncio
    async def test_resume_turn_failure_after_audit_reverts_choice(self, monkeypatch) -> None:
        """If anything after the audit raises (history load, LLM, persist),
        the orchestrator MUST call revert_clarification_to_pending so the
        user can retry rather than being permanently stuck at status='chosen'.
        """
        from unittest.mock import AsyncMock as _AM

        from app.services.chat import orchestrator as _orch
        from app.services.chat.plan_mode.short_circuit import PlanModeChoiceResult

        message_id = uuid.uuid4()

        # 1. Stub handle_plan_mode_choice to return success with a known
        #    chat_message_id we can match in the revert mock.
        fake_pmc_result = PlanModeChoiceResult(
            chosen_option={"id": "A", "source": "netsuite", "title": "GL"},
            chosen_source="netsuite",
            system_directive="## PRIOR CLARIFICATIONS\n...",
            chat_message_id=message_id,
        )

        async def _fake_handle(**kwargs):
            return fake_pmc_result

        # The orchestrator imports handle_plan_mode_choice INSIDE the
        # function body, so patch the source module.
        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.handle_plan_mode_choice",
            _fake_handle,
        )

        # 2. log_event SUCCEEDS — we want to test downstream failure, NOT
        #    audit failure (round 6 covered that case).
        async def _fake_log_event(**kwargs):
            return MagicMock()

        monkeypatch.setattr(_orch, "log_event", _fake_log_event)

        # 3. Force a downstream failure. `sanitize_user_input` is called
        #    early in the resumed turn body (line ~1476). Making it raise
        #    simulates ANY downstream failure (LLM error, tool error,
        #    persist failure) — they all need to trigger revert.
        def _boom_sanitize(text):
            raise RuntimeError("simulated downstream failure mid-resume-turn")

        monkeypatch.setattr(_orch, "sanitize_user_input", _boom_sanitize)

        # 4. Capture revert calls.
        revert_calls = []

        async def _fake_revert(**kwargs):
            revert_calls.append(kwargs)
            return True

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.revert_clarification_to_pending",
            _fake_revert,
        )

        # 5. Drive run_chat_turn down the plan_mode_choice path. The body
        #    is expected to raise (we set up _boom_sanitize), but BEFORE
        #    raising it MUST have called the revert.
        sess = _build_session()
        db = _AM()

        # Make supersede_pending_clarifications a no-op too — though we're
        # on the resume path so it shouldn't be called anyway.
        async def _fake_supersede(**kwargs):
            return []

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.supersede_pending_clarifications",
            _fake_supersede,
        )

        plan_mode_choice = {
            "action": "approve",
            "confirmation_id": str(message_id),
            "option_id": "A",
        }

        with pytest.raises(Exception):
            async for _chunk in _orch.run_chat_turn(
                db=db,
                session=sess,
                user_message="resume turn",
                user_id=uuid.uuid4(),
                tenant_id=sess.tenant_id,
                user_msg=MagicMock(id=uuid.uuid4()),
                plan_mode_choice=plan_mode_choice,
            ):
                pass

        # The crucial assertion: revert was called.
        assert revert_calls, (
            "After handle_plan_mode_choice succeeded and the audit fired, a "
            "downstream failure (simulated via sanitize_user_input) MUST "
            "trigger revert_clarification_to_pending so the user can retry. "
            "Without this, the choice is locked in at status='chosen' with "
            "no answer produced — round 7 Bug 1."
        )
        assert revert_calls[0]["message_id"] == message_id


class TestAuditFailureRollbackOrder:
    """Round 7 Bug 2 — when log_event raises during its flush, the SQLAlchemy
    session is left in a failed-transaction state. The existing revert path
    calls revert_clarification_to_pending on the same session BEFORE
    rollback, which will raise PendingRollbackError and the revert never
    actually executes — clarification stays at status='chosen'.

    Fix: the orchestrator MUST call ``await db.rollback()`` BEFORE the
    revert.
    """

    @pytest.mark.asyncio
    async def test_audit_failure_rolls_back_before_revert(self, monkeypatch) -> None:
        """Order check: db.rollback() is called BEFORE
        revert_clarification_to_pending() in the audit-failure path.
        """
        from unittest.mock import AsyncMock as _AM

        from app.services.chat import orchestrator as _orch
        from app.services.chat.plan_mode.short_circuit import PlanModeChoiceResult

        message_id = uuid.uuid4()
        fake_pmc_result = PlanModeChoiceResult(
            chosen_option={"id": "A", "source": "netsuite", "title": "GL"},
            chosen_source="netsuite",
            system_directive="## PRIOR CLARIFICATIONS\n...",
            chat_message_id=message_id,
        )

        async def _fake_handle(**kwargs):
            return fake_pmc_result

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.handle_plan_mode_choice",
            _fake_handle,
        )

        # log_event raises — simulating audit flush failure.
        async def _fake_log_event(**kwargs):
            raise RuntimeError("simulated audit flush failure")

        monkeypatch.setattr(_orch, "log_event", _fake_log_event)

        # Track call order across db.rollback and the revert helper.
        call_order: list[str] = []

        sess = _build_session()
        db = _AM()

        async def _track_rollback(*args, **kwargs):
            call_order.append("rollback")

        db.rollback = _track_rollback

        async def _fake_revert(**kwargs):
            call_order.append("revert")
            return True

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.revert_clarification_to_pending",
            _fake_revert,
        )

        plan_mode_choice = {
            "action": "approve",
            "confirmation_id": str(message_id),
            "option_id": "A",
        }

        with pytest.raises(Exception):
            async for _chunk in _orch.run_chat_turn(
                db=db,
                session=sess,
                user_message="resume turn",
                user_id=uuid.uuid4(),
                tenant_id=sess.tenant_id,
                user_msg=MagicMock(id=uuid.uuid4()),
                plan_mode_choice=plan_mode_choice,
            ):
                pass

        assert "rollback" in call_order, (
            "After log_event raises, the orchestrator MUST call "
            "db.rollback() to clear the failed-transaction state before "
            "running revert_clarification_to_pending — otherwise the "
            "revert query raises PendingRollbackError and the row stays "
            "stranded at status='chosen'. (round 7 Bug 2)"
        )
        assert "revert" in call_order, "revert must still run after rollback"
        # Order: rollback BEFORE revert.
        assert call_order.index("rollback") < call_order.index("revert"), (
            f"db.rollback must run BEFORE revert_clarification_to_pending. Actual order: {call_order}"
        )


# ---------------------------------------------------------------------------
# Round 8 Bug 1 — the existing wrap catches `Exception` only. On Python 3.11+
# `asyncio.CancelledError` does NOT inherit from `Exception` (it's a
# `BaseException`). When the outer `asyncio.wait_for(_run_chat_background,
# timeout=300)` cancellation fires, the wrap is bypassed and the
# clarification stays at status='chosen' despite the failed turn.
#
# Fix: catch `(Exception, asyncio.CancelledError)` explicitly. Run revert,
# then re-raise (cancellation MUST propagate).
# ---------------------------------------------------------------------------


class TestResumeTurnCancelledReverts:
    """Round 8 Bug 1 — `asyncio.CancelledError` (raised by outer
    `asyncio.wait_for` timeout) must trigger `revert_clarification_to_pending`
    and then re-raise so cancellation propagates to the asyncio loop.
    """

    @pytest.mark.asyncio
    async def test_resume_turn_cancelled_reverts_choice(self, monkeypatch) -> None:
        """If the resume turn body is cancelled (e.g. via
        ``asyncio.wait_for`` timeout), the orchestrator MUST call
        ``revert_clarification_to_pending`` so the user can retry — AND
        the ``CancelledError`` must propagate to the caller (it's not an
        ordinary error; the loop needs to know cancellation happened).
        """
        from unittest.mock import AsyncMock as _AM

        from app.services.chat import orchestrator as _orch
        from app.services.chat.plan_mode.short_circuit import PlanModeChoiceResult

        message_id = uuid.uuid4()

        fake_pmc_result = PlanModeChoiceResult(
            chosen_option={"id": "A", "source": "netsuite", "title": "GL"},
            chosen_source="netsuite",
            system_directive="## PRIOR CLARIFICATIONS\n...",
            chat_message_id=message_id,
        )

        async def _fake_handle(**kwargs):
            return fake_pmc_result

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.handle_plan_mode_choice",
            _fake_handle,
        )

        # log_event SUCCEEDS — we want the cancel to come AFTER audit.
        async def _fake_log_event(**kwargs):
            return MagicMock()

        monkeypatch.setattr(_orch, "log_event", _fake_log_event)

        # Force a CancelledError mid-resume-turn (simulates outer
        # asyncio.wait_for(timeout=300) firing). Use sanitize_user_input
        # like the round-7 test.
        def _boom_cancel(text):
            raise asyncio.CancelledError("simulated cancellation mid-resume-turn")

        monkeypatch.setattr(_orch, "sanitize_user_input", _boom_cancel)

        revert_calls: list[dict] = []

        async def _fake_revert(**kwargs):
            revert_calls.append(kwargs)
            return True

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.revert_clarification_to_pending",
            _fake_revert,
        )

        async def _fake_supersede(**kwargs):
            return []

        monkeypatch.setattr(
            "app.services.chat.plan_mode.short_circuit.supersede_pending_clarifications",
            _fake_supersede,
        )

        sess = _build_session()
        db = _AM()

        plan_mode_choice = {
            "action": "approve",
            "confirmation_id": str(message_id),
            "option_id": "A",
        }

        # CancelledError MUST propagate (cancellation isn't a normal error;
        # callers and the asyncio loop need to see it).
        with pytest.raises(asyncio.CancelledError):
            async for _chunk in _orch.run_chat_turn(
                db=db,
                session=sess,
                user_message="resume turn",
                user_id=uuid.uuid4(),
                tenant_id=sess.tenant_id,
                user_msg=MagicMock(id=uuid.uuid4()),
                plan_mode_choice=plan_mode_choice,
            ):
                pass

        # Crucial: revert was called BEFORE the CancelledError propagated —
        # otherwise the clarification is stranded at status='chosen' and
        # the user gets 409 on retry.
        assert revert_calls, (
            "After handle_plan_mode_choice succeeded and the audit fired, a "
            "CancelledError (from outer asyncio.wait_for) MUST trigger "
            "revert_clarification_to_pending. The current `except Exception` "
            "wrap misses CancelledError on Python 3.11+ — it doesn't inherit "
            "from Exception. Round 8 Bug 1."
        )
        assert revert_calls[0]["message_id"] == message_id
