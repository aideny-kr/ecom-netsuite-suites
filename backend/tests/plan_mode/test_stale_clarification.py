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
    """No pending clarifications → returns 0, no commit, no add."""
    db = _make_db(pending_msgs=[])
    result = await supersede_pending_clarifications(
        session_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert result == 0
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
    assert result == 1
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
    assert result == 3
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
    assert result == 0
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
    assert result == 1
