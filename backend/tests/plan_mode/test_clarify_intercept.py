"""Tests for clarify tool-call interceptor."""

from unittest.mock import AsyncMock

import pytest

from app.services.chat.plan_mode.clarify_intercept import (
    InterceptError,
    InterceptResult,
    intercept_clarify_call,
)

_VALID_INPUT = {
    "options": [
        {
            "id": "A",
            "title": "NetSuite GL",
            "rationale": "recognized revenue",
            "source": "netsuite",
            "is_default": True,
        },
        {
            "id": "B",
            "title": "BigQuery checkout",
            "rationale": "ecommerce totals",
            "source": "bigquery",
            "is_default": False,
        },
    ],
    "ambiguity_summary": "Revenue can mean two things — recognized GL or checkout totals.",
}


@pytest.mark.asyncio
async def test_valid_input_returns_persist_directive():
    db = AsyncMock()
    result = await intercept_clarify_call(
        tool_input=_VALID_INPUT,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=db,
    )
    assert isinstance(result, InterceptResult)
    so = result.structured_output
    assert so["type"] == "clarification"
    assert so["status"] == "pending"
    assert "confirmation_token" in so
    assert so["default_id"] == "A"
    assert so["ambiguity_summary"] == _VALID_INPUT["ambiguity_summary"]
    assert len(so["options"]) == 2
    # SSE payload mirrors structured_output for now
    assert result.sse_payload == so


@pytest.mark.asyncio
async def test_no_default_marked_returns_error():
    """Schema requires exactly one is_default=True; reject if zero."""
    bad_input = dict(_VALID_INPUT)
    bad_input["options"] = [{**o, "is_default": False} for o in _VALID_INPUT["options"]]
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)
    assert "default" in result.error_message.lower()


@pytest.mark.asyncio
async def test_two_defaults_returns_error():
    bad_input = dict(_VALID_INPUT)
    bad_input["options"] = [{**o, "is_default": True} for o in _VALID_INPUT["options"]]
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)
    assert "default" in result.error_message.lower()


@pytest.mark.asyncio
async def test_disconnected_source_filtered_out():
    """If only NetSuite is connected, BigQuery option is dropped."""
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite GL", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
            {"id": "C", "title": "Shopify", "rationale": "Shop", "source": "shopify", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "shopify"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult)
    sources = [o["source"] for o in result.structured_output["options"]]
    assert "bigquery" not in sources
    assert "netsuite" in sources
    assert "shopify" in sources


@pytest.mark.asyncio
async def test_too_few_connected_options_returns_error():
    """If fewer than 2 connected options remain after filter, reject."""
    bad_input = {
        "options": [
            {"id": "A", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": True},
            {"id": "B", "title": "Shopify", "rationale": "Shop", "source": "shopify", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite"],  # neither bigquery nor shopify connected
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)
    assert "connected" in result.error_message.lower()


@pytest.mark.asyncio
async def test_too_few_options_in_input():
    """Schema requires 2-3 options; reject 1-option input."""
    bad_input = {
        "options": [
            {"id": "A", "title": "x", "rationale": "y", "source": "netsuite", "is_default": True},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)


@pytest.mark.asyncio
async def test_too_many_options_in_input():
    """Schema requires 2-3 options; reject 4-option input."""
    bad_input = {
        "options": [
            {"id": "A", "title": "x", "rationale": "y", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "x", "rationale": "y", "source": "bigquery", "is_default": False},
            {"id": "C", "title": "x", "rationale": "y", "source": "shopify", "is_default": False},
            {"id": "C", "title": "x", "rationale": "y", "source": "stripe", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery", "shopify", "stripe"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)


@pytest.mark.asyncio
async def test_missing_ambiguity_summary():
    bad_input = {**_VALID_INPUT, "ambiguity_summary": ""}
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)
    assert "summary" in result.error_message.lower()


@pytest.mark.asyncio
async def test_hmac_token_is_64_char_hex_session_bound():
    """Token is HMAC-SHA256 hex digest (64 chars), bound to session_id + payload."""
    db = AsyncMock()
    result = await intercept_clarify_call(
        tool_input=_VALID_INPUT,
        session_id="sess-abc",
        active_connectors=["netsuite", "bigquery"],
        db=db,
    )
    assert isinstance(result, InterceptResult)
    token = result.structured_output["confirmation_token"]
    assert isinstance(token, str)
    assert len(token) == 64
    # Hex characters only
    int(token, 16)


@pytest.mark.asyncio
async def test_hmac_token_event_type_isolated_from_write_confirm():
    """Plan-mode tokens cannot be replayed as write-confirm tokens."""
    from app.services.chat.mutation_guard import verify_confirmation_token

    result = await intercept_clarify_call(
        tool_input=_VALID_INPUT,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult)
    token = result.structured_output["confirmation_token"]

    # Plan-mode token is bound to event_type='plan_mode_choice'
    # Reconstruct the payload-for-hmac the way intercept does it:
    import json as _json

    payload_for_hmac = _json.dumps(
        {
            "options": result.structured_output["options"],
            "default_id": result.structured_output["default_id"],
        },
        sort_keys=True,
    )
    # Default verification (write_confirm) must REJECT the token
    assert verify_confirmation_token(token, "sess-1", payload_for_hmac) is False
    # Plan-mode verification must ACCEPT
    assert verify_confirmation_token(token, "sess-1", payload_for_hmac, event_type="plan_mode_choice") is True


@pytest.mark.asyncio
async def test_expires_at_iso_8601_utc():
    """expires_at is a 5-minute future timestamp in ISO-8601 UTC."""
    from datetime import datetime, timezone

    db = AsyncMock()
    before = datetime.now(timezone.utc).timestamp()
    result = await intercept_clarify_call(
        tool_input=_VALID_INPUT,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=db,
    )
    after = datetime.now(timezone.utc).timestamp()
    assert isinstance(result, InterceptResult)
    expires_at = result.structured_output["expires_at"]
    parsed = datetime.fromisoformat(expires_at).timestamp()
    # 5 minutes after now() at call time
    assert before + 290 <= parsed <= after + 310
