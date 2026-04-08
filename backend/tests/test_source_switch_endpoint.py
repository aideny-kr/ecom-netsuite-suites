"""Integration tests for the source-switch command in send_message.

Task 12: when a user sends "use BigQuery" / "use NetSuite", the chat endpoint
must update `chat_sessions.source_pin` and either:

  (a) re-run the previous user query against the new source if the smart-rerun
      guards pass (previous turn used a data tool, has a disclosure block,
      and `can_switch_source` was True), OR
  (b) emit a single acknowledgment message and terminate the run.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select, text

from app.models.chat import ChatMessage, ChatSession
from app.models.chat_disclosure_event import ChatDisclosureEvent

# ---------------------------------------------------------------------------
# Helper: enable the disclosure_footer_enabled feature flag for a tenant
# ---------------------------------------------------------------------------


async def _enable_disclosure_flag(db, tenant_id) -> None:
    """Enable the disclosure_footer_enabled flag for a tenant and clear the TTL cache.

    The endpoint-side logic is gated on this flag; tests that exercise the
    source-switch / pushback / ack paths must flip it on before issuing the
    request.
    """
    from app.services.feature_flag_service import clear_cache

    await db.execute(
        text(
            "INSERT INTO tenant_feature_flags (tenant_id, flag_key, enabled, created_at, updated_at) "
            "VALUES (:tid, 'disclosure_footer_enabled', true, NOW(), NOW()) "
            "ON CONFLICT ON CONSTRAINT uq_tenant_feature_flag DO UPDATE SET enabled = true"
        ),
        {"tid": str(tenant_id)},
    )
    await db.commit()
    clear_cache()


# ---------------------------------------------------------------------------
# Helper: build a session with prior history that satisfies the rerun guards
# ---------------------------------------------------------------------------


async def _seed_session_with_history(
    db,
    user,
    *,
    can_switch: bool,
    has_tool_calls: bool,
    has_disclosure: bool,
) -> tuple[ChatSession, ChatMessage, ChatMessage]:
    """Create a session with one prior user->assistant turn.

    Returns (session, prev_user_msg, prev_assistant_msg).
    """
    session = ChatSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        title="Source Switch Test",
    )
    db.add(session)
    await db.flush()

    # Make the prior user message "older" by stamping a fixed earlier timestamp.
    base = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
    prev_user = ChatMessage(
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="user",
        content="What were sales last week?",
        created_at=base,
    )
    db.add(prev_user)
    await db.flush()

    disclosure_payload = None
    if has_disclosure:
        disclosure_payload = {
            "source": "netsuite",
            "interpretation": "Sales last week.",
            "implicit_filters": [],
            "can_switch_source": can_switch,
            "is_rerun": False,
            "failure_mode": False,
        }

    tool_calls = None
    if has_tool_calls:
        tool_calls = [
            {
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT 1 FROM transaction"},
                "result_summary": "1 row",
                "duration_ms": 5,
            }
        ]

    prev_assistant = ChatMessage(
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="assistant",
        content="Sales were $100.",
        tool_calls=tool_calls,
        disclosure_json=disclosure_payload,
        created_at=base.replace(second=1),
    )
    db.add(prev_assistant)
    await db.commit()

    return session, prev_user, prev_assistant


# ---------------------------------------------------------------------------
# Helper: minimal RunManager mock that records writes
# ---------------------------------------------------------------------------


def _make_mock_rm(*, available: bool = True):
    rm = MagicMock()
    rm.available = available
    rm.get_active_run.return_value = None
    rm.get_status.return_value = None
    rm.events = []

    def _write_event(run_id, event):
        rm.events.append((run_id, event))

    rm.write_event.side_effect = _write_event
    return rm


# ---------------------------------------------------------------------------
# Test: "use BigQuery" updates source_pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_bigquery_updates_source_pin(client, db, admin_user):
    """Sending 'use BigQuery' should set chat_sessions.source_pin to 'bigquery'."""
    user, headers = admin_user
    await _enable_disclosure_flag(db, user.tenant_id)
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    session, _, _ = await _seed_session_with_history(
        db, user, can_switch=False, has_tool_calls=False, has_disclosure=False
    )
    session_id = str(session.id)

    mock_rm = _make_mock_rm(available=True)

    async def mock_generator(**kwargs):  # never invoked when guards fail (ack path)
        yield {"type": "text", "content": "should not run"}

    with (
        patch("app.api.v1.chat.run_chat_turn", side_effect=mock_generator),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "use BigQuery"},
            headers=headers,
        )

    assert resp.status_code == 202

    # Refetch session to verify pin updated
    result = await db.execute(select(ChatSession).where(ChatSession.id == session.id))
    refreshed = result.scalar_one()
    assert refreshed.source_pin == "bigquery"


# ---------------------------------------------------------------------------
# Test: "use NetSuite" after a BigQuery pin updates the pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_netsuite_after_bigquery_updates_pin(client, db, admin_user):
    """Switching back to NetSuite should update the pin to 'netsuite'."""
    user, headers = admin_user
    await _enable_disclosure_flag(db, user.tenant_id)
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    session, _, _ = await _seed_session_with_history(
        db, user, can_switch=False, has_tool_calls=False, has_disclosure=False
    )
    session.source_pin = "bigquery"
    await db.commit()
    session_id = str(session.id)

    mock_rm = _make_mock_rm(available=True)

    async def mock_generator(**kwargs):
        yield {"type": "text", "content": "should not run"}

    with (
        patch("app.api.v1.chat.run_chat_turn", side_effect=mock_generator),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "use NetSuite"},
            headers=headers,
        )

    assert resp.status_code == 202

    result = await db.execute(select(ChatSession).where(ChatSession.id == session.id))
    refreshed = result.scalar_one()
    assert refreshed.source_pin == "netsuite"


# ---------------------------------------------------------------------------
# Test: partial / embedded match does NOT trigger a switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_match_does_not_fire(client, db, admin_user):
    """'let me use BigQuery to find X' should NOT trigger a source switch."""
    user, headers = admin_user
    await _enable_disclosure_flag(db, user.tenant_id)
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    session, _, _ = await _seed_session_with_history(
        db, user, can_switch=False, has_tool_calls=False, has_disclosure=False
    )
    session_id = str(session.id)

    mock_rm = _make_mock_rm(available=True)

    async def mock_generator(**kwargs):
        yield {"type": "text", "content": "answering normally"}

    with (
        patch("app.api.v1.chat.run_chat_turn", side_effect=mock_generator),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "let me use BigQuery to find the answer"},
            headers=headers,
        )

    assert resp.status_code == 202

    result = await db.execute(select(ChatSession).where(ChatSession.id == session.id))
    refreshed = result.scalar_one()
    assert refreshed.source_pin is None  # unchanged


# ---------------------------------------------------------------------------
# Test: failed guards => acknowledgment message persisted, no rerun
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_with_failed_guards_emits_ack_only(client, db, admin_user):
    """When the previous turn lacks tool_calls or disclosure, emit an ack."""
    user, headers = admin_user
    await _enable_disclosure_flag(db, user.tenant_id)
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    # Prior turn has NO tool_calls and NO disclosure → guards fail
    session, _, _ = await _seed_session_with_history(
        db, user, can_switch=False, has_tool_calls=False, has_disclosure=False
    )
    session_id = str(session.id)

    mock_rm = _make_mock_rm(available=True)
    rerun_called = {"value": False}

    async def mock_generator(**kwargs):
        rerun_called["value"] = True
        yield {"type": "text", "content": "should not run"}

    with (
        patch("app.api.v1.chat.run_chat_turn", side_effect=mock_generator),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "use BigQuery"},
            headers=headers,
        )

    assert resp.status_code == 202
    body = resp.json()
    assert "run_id" in body
    assert body["session_id"] == session_id

    # No background re-run should have been spawned
    assert rerun_called["value"] is False

    # An ack assistant message should have been persisted
    result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.session_id == session.id,
            ChatMessage.role == "assistant",
        )
    )
    assistant_msgs = result.scalars().all()
    # At least one ack message — text should mention BigQuery (proper casing)
    ack_texts = [m.content for m in assistant_msgs if "BigQuery" in m.content]
    assert ack_texts, f"Expected an ack message mentioning BigQuery, got: {[m.content for m in assistant_msgs]}"

    # Mock RunManager should have received a single message event + status complete
    assert any(ev[1].get("type") == "message" for ev in mock_rm.events)
    mock_rm.set_status.assert_called()  # at minimum the terminal status was set


# ---------------------------------------------------------------------------
# Test: smart re-run path — guards pass → background task runs with rerun text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_with_passing_guards_triggers_rerun(client, db, admin_user):
    """When all 5 guards pass, the background task is spawned with the previous user message."""
    user, headers = admin_user
    await _enable_disclosure_flag(db, user.tenant_id)
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    session, prev_user, _ = await _seed_session_with_history(
        db, user, can_switch=True, has_tool_calls=True, has_disclosure=True
    )
    session_id = str(session.id)
    prev_user_content = prev_user.content

    mock_rm = _make_mock_rm(available=True)

    # Use a MagicMock so we can read call_args.kwargs synchronously after the
    # request returns. The side_effect coroutine is a no-op — MagicMock records
    # the call kwargs at invocation time, before the coroutine runs.
    mock_bg = MagicMock()

    async def fake_background(*args, **kwargs):
        return None  # no-op coroutine

    mock_bg.side_effect = fake_background

    with (
        patch("app.api.v1.chat._run_chat_background", mock_bg),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "use BigQuery"},
            headers=headers,
        )

    assert resp.status_code == 202

    # Pin updated
    result = await db.execute(select(ChatSession).where(ChatSession.id == session.id))
    refreshed = result.scalar_one()
    assert refreshed.source_pin == "bigquery"

    # Background task got the previous user message text + is_rerun=True.
    # Read kwargs synchronously from the mock — MagicMock records call_args
    # at invocation time, independent of event-loop scheduling.
    assert mock_bg.call_args is not None, "_run_chat_background was not invoked"
    assert mock_bg.call_args.kwargs["user_message"] == prev_user_content
    assert mock_bg.call_args.kwargs["is_rerun"] is True


# ---------------------------------------------------------------------------
# Test: flag-OFF invariant — "use BigQuery" is a pass-through, no mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_switch_is_noop_when_flag_off(client, db, admin_user):
    """When disclosure_footer_enabled is OFF, `use BigQuery` must NOT mutate source_pin,
    must NOT write telemetry, and MUST pass through to the background task with the
    literal user message.
    """
    user, headers = admin_user
    # Do NOT enable the flag — clear cache just in case a prior test leaked state.
    from app.services.feature_flag_service import clear_cache

    clear_cache()

    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    # Seed a session with a full prior turn — if the gate leaked, the rerun
    # path would fire and mutate state, so this gives the test teeth.
    session, _, _ = await _seed_session_with_history(
        db, user, can_switch=True, has_tool_calls=True, has_disclosure=True
    )
    session_id = str(session.id)

    mock_rm = _make_mock_rm(available=True)

    # MagicMock captures call_args synchronously at invocation time.
    mock_bg = MagicMock()

    async def fake_background(*args, **kwargs):
        return None

    mock_bg.side_effect = fake_background

    with (
        patch("app.api.v1.chat._run_chat_background", mock_bg),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "use BigQuery"},
            headers=headers,
        )

    assert resp.status_code == 202

    # source_pin must remain None — pin NOT mutated when flag is off
    result = await db.execute(select(ChatSession).where(ChatSession.id == session.id))
    refreshed = result.scalar_one()
    assert refreshed.source_pin is None

    # No telemetry rows for this session
    result = await db.execute(
        select(ChatDisclosureEvent).where(ChatDisclosureEvent.session_id == session.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 0, f"Expected no telemetry rows, got {[r.event_type for r in rows]}"

    # Background task invoked with the literal "use BigQuery" text and is_rerun=False
    assert mock_bg.call_args is not None, "_run_chat_background was not invoked"
    assert mock_bg.call_args.kwargs["user_message"] == "use BigQuery"
    assert mock_bg.call_args.kwargs["is_rerun"] is False
