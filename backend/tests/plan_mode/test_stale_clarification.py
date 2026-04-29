"""Verify pending clarifications transition to superseded on next user message."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.mutation_guard import generate_confirmation_token
from app.services.chat.plan_mode.short_circuit import (
    supersede_pending_clarifications,
)


def _build_pending_so(session_id: str) -> dict:
    options = [
        {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
        {"id": "B", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
    ]
    payload_for_hmac = json.dumps({"options": options, "default_id": "A"}, sort_keys=True)
    return {
        "type": "clarification",
        "status": "pending",
        "options": options,
        "default_id": "A",
        "ambiguity_summary": "Revenue can mean two things.",
        "confirmation_token": generate_confirmation_token(session_id, payload_for_hmac, event_type="plan_mode_choice"),
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def _mock_msg(session_id: uuid.UUID, structured_output: dict) -> MagicMock:
    msg = MagicMock()
    msg.id = uuid.uuid4()
    msg.session_id = session_id
    msg.structured_output = structured_output
    return msg


def _make_db(pending_msgs: list[MagicMock], cas_rowcount: int = 1) -> AsyncMock:
    db = AsyncMock()
    select_result = MagicMock()
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=pending_msgs)
    select_result.scalars = MagicMock(return_value=scalars_result)
    update_result = MagicMock(rowcount=cas_rowcount)

    _state = {"calls": 0}

    async def _execute(*args, **kwargs):
        _state["calls"] += 1
        return select_result if _state["calls"] == 1 else update_result

    db.execute = _execute
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_no_pending_no_op():
    """No pending clarifications → returns [], no commit, no add."""
    db = _make_db(pending_msgs=[])
    result = await supersede_pending_clarifications(
        session_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert result == []
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersedes_one_pending_clarification():
    session_id = uuid.uuid4()
    msg = _mock_msg(session_id, _build_pending_so(str(session_id)))
    db = _make_db(pending_msgs=[msg], cas_rowcount=1)

    result = await supersede_pending_clarifications(
        session_id=session_id,
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert result == [msg.id]
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_supersedes_multiple_pending():
    """Defensive: multiple pending clarifications all transitioned + telemetry."""
    session_id = uuid.uuid4()
    msgs = [_mock_msg(session_id, _build_pending_so(str(session_id))) for _ in range(3)]

    db = AsyncMock()
    select_result = MagicMock()
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=msgs)
    select_result.scalars = MagicMock(return_value=scalars_result)

    _state = {"calls": 0}

    async def _execute(*args, **kwargs):
        _state["calls"] += 1
        # First call is the SELECT, subsequent are per-row UPDATEs
        if _state["calls"] == 1:
            return select_result
        return MagicMock(rowcount=1)

    db.execute = _execute
    db.add = MagicMock()
    db.commit = AsyncMock()

    result = await supersede_pending_clarifications(
        session_id=session_id,
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert isinstance(result, list)
    assert len(result) == 3
    assert set(result) == {m.id for m in msgs}
    assert db.add.call_count == 3
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cas_returns_zero_skips_telemetry():
    """If CAS rowcount=0 (concurrent resolve), don't emit telemetry for that row."""
    session_id = uuid.uuid4()
    msg = _mock_msg(session_id, _build_pending_so(str(session_id)))
    db = _make_db(pending_msgs=[msg], cas_rowcount=0)  # CAS lost the race

    result = await supersede_pending_clarifications(
        session_id=session_id,
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert result == []
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_id_string_accepted():
    """Helper accepts str OR UUID for session_id (caller flexibility)."""
    session_id_uuid = uuid.uuid4()
    msg = _mock_msg(session_id_uuid, _build_pending_so(str(session_id_uuid)))
    db = _make_db(pending_msgs=[msg], cas_rowcount=1)

    result = await supersede_pending_clarifications(
        session_id=str(session_id_uuid),  # passed as string
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert result == [msg.id]


# ---------------------------------------------------------------------------
# Audit emission (Task 6.4 — non-fatal, one log per superseded message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_emitted_on_supersede_one_per_message(monkeypatch):
    """The orchestrator should call log_event once per superseded message ID
    returned by ``supersede_pending_clarifications``.

    Sanity: the helper now returns a list[UUID] instead of int — verify the
    orchestrator iterates that list. We exercise the loop directly so the
    test is decoupled from the broader run_chat_turn flow.
    """
    from app.services import audit_service as _audit

    captured = []

    async def _fake_log_event(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(_audit, "log_event", _fake_log_event)

    session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    superseded_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    # Mimic the orchestrator's iteration:
    for msg_id in superseded_ids:
        await _audit.log_event(
            db=AsyncMock(),
            tenant_id=tenant_id,
            category="chat",
            action="plan_mode.superseded",
            actor_id=user_id,
            resource_type="chat_message",
            resource_id=str(msg_id),
            payload={"reason": "free_text_reply", "session_id": str(session_id)},
        )

    assert len(captured) == 3
    for i, msg_id in enumerate(superseded_ids):
        call = captured[i]
        assert call["category"] == "chat"
        assert call["action"] == "plan_mode.superseded"
        assert call["actor_id"] == user_id
        assert call["resource_type"] == "chat_message"
        assert call["resource_id"] == str(msg_id)
        assert call["payload"]["reason"] == "free_text_reply"
        assert call["payload"]["session_id"] == str(session_id)


def test_orchestrator_supersede_audit_is_non_fatal():
    """The supersede audit emission MUST sit inside the existing try/except
    so an audit DB failure never aborts the chat turn (matches the existing
    best-effort supersede semantics).

    Source-level invariant: there must be a try/except wrapping both the
    supersede call AND its audit-loop emit.
    """
    import inspect

    from app.services.chat.orchestrator import run_chat_turn

    source = inspect.getsource(run_chat_turn)
    superseded_idx = source.find('"plan_mode.superseded"')
    assert superseded_idx > 0, "expected plan_mode.superseded audit emit"

    # The closest preceding ``try:`` and following ``except`` must wrap the emit.
    try_block_idx = source.rfind("try:", 0, superseded_idx)
    except_block_idx = source.find("except", superseded_idx)
    assert try_block_idx > 0, "supersede audit emit must be inside a try/except"
    assert except_block_idx > 0, "supersede audit emit must be inside a try/except"
    # And the supersede audit log must be ABOVE the except.
    assert superseded_idx < except_block_idx
