"""Unit tests for handle_plan_mode_choice short-circuit."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.mutation_guard import generate_confirmation_token
from app.services.chat.plan_mode.short_circuit import (
    PlanModeChoiceError,
    PlanModeChoiceResult,
    handle_plan_mode_choice,
)


def _build_so(session_id: str, options: list[dict] | None = None, default_id: str = "A") -> dict:
    """Build a structured_output dict with a real HMAC token bound to it."""
    options = options or [
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
    payload_for_hmac = json.dumps({"options": options, "default_id": default_id}, sort_keys=True)
    token = generate_confirmation_token(session_id, payload_for_hmac, event_type="plan_mode_choice")
    return {
        "type": "clarification",
        "status": "pending",
        "options": options,
        "default_id": default_id,
        "ambiguity_summary": "Revenue can mean two things.",
        "confirmation_token": token,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def _mock_msg(session_id: str, structured_output: dict | None) -> MagicMock:
    msg = MagicMock()
    msg.id = uuid.uuid4()
    msg.session_id = uuid.UUID(session_id)
    msg.structured_output = structured_output
    return msg


def _mock_db_with_msg(msg: MagicMock | None, cas_rowcount: int = 1) -> AsyncMock:
    db = AsyncMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none = MagicMock(return_value=msg)
    update_result = MagicMock(rowcount=cas_rowcount)

    _calls = {"n": 0}

    async def _execute(*args, **kwargs):
        _calls["n"] += 1
        return select_result if _calls["n"] == 1 else update_result

    db.execute = _execute
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_valid_approve_returns_result_and_directive():
    session_id = str(uuid.uuid4())
    so = _build_so(session_id)
    msg = _mock_msg(session_id, so)
    msg_id = msg.id
    db = _mock_db_with_msg(msg)

    result = await handle_plan_mode_choice(
        plan_mode_choice={
            "action": "approve",
            "confirmation_id": str(msg_id),
            "option_id": "A",
        },
        session_id=session_id,
        tenant_id=uuid.uuid4(),
        db=db,
    )

    assert isinstance(result, PlanModeChoiceResult)
    assert result.chosen_source == "netsuite"
    assert result.chosen_option["id"] == "A"
    assert "PRIOR CLARIFICATIONS" in result.system_directive
    assert "NetSuite GL" in result.system_directive

    # CAS issued + ChatDisclosureEvent inserted + committed
    db.add.assert_called_once()
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_action_must_be_approve():
    db = AsyncMock()
    result = await handle_plan_mode_choice(
        plan_mode_choice={
            "action": "reject",
            "confirmation_id": str(uuid.uuid4()),
            "option_id": "A",
        },
        session_id=str(uuid.uuid4()),
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert isinstance(result, PlanModeChoiceError)
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_invalid_option_id():
    db = AsyncMock()
    result = await handle_plan_mode_choice(
        plan_mode_choice={
            "action": "approve",
            "confirmation_id": str(uuid.uuid4()),
            "option_id": "Z",
        },
        session_id=str(uuid.uuid4()),
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert isinstance(result, PlanModeChoiceError)


@pytest.mark.asyncio
async def test_message_not_found():
    db = _mock_db_with_msg(msg=None)
    result = await handle_plan_mode_choice(
        plan_mode_choice={
            "action": "approve",
            "confirmation_id": str(uuid.uuid4()),
            "option_id": "A",
        },
        session_id=str(uuid.uuid4()),
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert isinstance(result, PlanModeChoiceError)
    assert result.status_code == 404


@pytest.mark.asyncio
async def test_session_mismatch():
    """confirmation_id must belong to the current session — cross-session replay rejected."""
    correct_session = str(uuid.uuid4())
    so = _build_so(correct_session)
    msg = _mock_msg(correct_session, so)
    db = _mock_db_with_msg(msg)

    result = await handle_plan_mode_choice(
        plan_mode_choice={
            "action": "approve",
            "confirmation_id": str(msg.id),
            "option_id": "A",
        },
        session_id=str(uuid.uuid4()),  # DIFFERENT session
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert isinstance(result, PlanModeChoiceError)
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_already_resolved():
    session_id = str(uuid.uuid4())
    so = _build_so(session_id)
    so["status"] = "chosen"
    so["chosen_id"] = "B"
    msg = _mock_msg(session_id, so)
    db = _mock_db_with_msg(msg)

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
    assert isinstance(result, PlanModeChoiceError)
    assert result.status_code == 409


@pytest.mark.asyncio
async def test_invalid_hmac_token():
    """Tampered structured_output -> token verification fails."""
    session_id = str(uuid.uuid4())
    so = _build_so(session_id)
    so["confirmation_token"] = "deadbeef" * 8  # 64 chars but wrong
    msg = _mock_msg(session_id, so)
    db = _mock_db_with_msg(msg)

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
    assert isinstance(result, PlanModeChoiceError)
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_concurrent_resolve_via_cas():
    """If CAS UPDATE returns rowcount=0, another resolve already happened."""
    session_id = str(uuid.uuid4())
    so = _build_so(session_id)
    msg = _mock_msg(session_id, so)
    db = _mock_db_with_msg(msg, cas_rowcount=0)

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
    assert isinstance(result, PlanModeChoiceError)
    assert result.status_code == 409


@pytest.mark.asyncio
async def test_option_id_not_in_options():
    """User picks 'C' but options only had A/B."""
    session_id = str(uuid.uuid4())
    so = _build_so(session_id)  # only A and B in options
    msg = _mock_msg(session_id, so)
    db = _mock_db_with_msg(msg)

    result = await handle_plan_mode_choice(
        plan_mode_choice={
            "action": "approve",
            "confirmation_id": str(msg.id),
            "option_id": "C",
        },
        session_id=session_id,
        tenant_id=uuid.uuid4(),
        db=db,
    )
    assert isinstance(result, PlanModeChoiceError)
