"""Tests for clarify tool-call interceptor."""

from unittest.mock import AsyncMock

import pytest

from app.services.chat.plan_mode.clarify_intercept import (
    InterceptError,
    InterceptResult,
    intercept_clarify_call,
)
from app.services.chat.plan_mode.source_resolver import (
    canonicalize_connector_providers,
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
    """If only NetSuite + Shopify are connected, BigQuery option is dropped.

    Round 8 Bug 2: bare 'shopify' (REST) no longer canonicalizes — only
    shopify_mcp does — so use the MCP provider here.
    """
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
        active_connectors=["netsuite", "shopify_mcp"],
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
async def test_raw_mcp_provider_strings_resolve_to_canonical_sources():
    """Regression: ``active_connectors`` may arrive as raw mcp_connector.provider
    strings (``netsuite_mcp``, ``shopify_mcp``) — not the canonical bare names
    used by the clarify schema (``netsuite``, ``shopify``).

    Without canonicalization, ``"netsuite" in ["netsuite_mcp", "bigquery"]``
    is False, every option drops, and the gate degrades to InterceptError.

    Caller site: ``base_agent.py`` builds ``active_connectors`` as
    ``[c.provider for c in self._connectors]`` where ``_connectors`` is loaded
    from ``mcp_connectors`` (provider literals: ``netsuite_mcp``,
    ``shopify_mcp``, ``bigquery``, ``google_sheets``, ``custom``).
    """
    db = AsyncMock()
    result = await intercept_clarify_call(
        tool_input=_VALID_INPUT,
        session_id="sess-1",
        # Real production data shape — raw mcp_connector.provider strings
        active_connectors=["netsuite_mcp", "bigquery"],
        db=db,
    )
    assert isinstance(result, InterceptResult), (
        f"Expected InterceptResult; got {type(result).__name__}: {getattr(result, 'error_message', '')}"
    )
    sources = [o["source"] for o in result.structured_output["options"]]
    assert "netsuite" in sources
    assert "bigquery" in sources


@pytest.mark.asyncio
async def test_canonicalize_handles_shopify_mcp_suffix():
    """``shopify_mcp`` provider must canonicalize to ``shopify``."""
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "Shopify", "rationale": "Shop", "source": "shopify", "is_default": False},
        ],
        "ambiguity_summary": "Revenue can mean recognized GL or Shopify orders.",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite_mcp", "shopify_mcp"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult), (
        f"Expected InterceptResult; got {type(result).__name__}: {getattr(result, 'error_message', '')}"
    )


@pytest.mark.asyncio
async def test_canonicalize_handles_rest_netsuite_provider():
    """REST NetSuite (``connections.provider == 'netsuite'``) is connected too —
    canonicalization must accept the bare ``netsuite`` provider string verbatim.
    """
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        # Mixed: NetSuite REST (bare) + BigQuery MCP
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult)


def test_stripe_mcp_provider_canonicalizes_to_stripe():
    """Codex round 5 P2 Bug 2: Stripe MCP tenants store their connector with
    ``mcp_connectors.provider == 'stripe_mcp'``. The map only contained
    bare ``'stripe'`` (REST). Without the alias entry, Stripe MCP tenants get
    Stripe options dropped from clarify intercept (and ext__<uuid>__* tools
    dropped on resume) — both call sites consume this same map.
    """
    result = canonicalize_connector_providers(["stripe_mcp"])
    assert result == {"stripe"}


def test_stripe_mcp_alongside_rest_stripe_canonicalizes():
    """Both stripe and stripe_mcp providers canonicalize to ``stripe``."""
    result = canonicalize_connector_providers(["stripe", "stripe_mcp"])
    assert result == {"stripe"}


@pytest.mark.asyncio
async def test_intercept_accepts_stripe_option_with_stripe_mcp_provider():
    """End-to-end: clarify intercept must accept a Stripe option when the
    only connected source is the ``stripe_mcp`` provider literal.
    """
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "Stripe", "rationale": "charges", "source": "stripe", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite_mcp", "stripe_mcp"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult), (
        f"Expected InterceptResult; got {type(result).__name__}: {getattr(result, 'error_message', '')}"
    )
    sources = [o["source"] for o in result.structured_output["options"]]
    assert "stripe" in sources
    assert "netsuite" in sources


@pytest.mark.asyncio
async def test_canonicalize_drops_disconnected_with_raw_providers():
    """Filtering still works after canonicalization — disconnected options drop."""
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
            {"id": "C", "title": "Shopify", "rationale": "Shop", "source": "shopify", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite_mcp", "shopify_mcp"],  # bigquery absent
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult)
    sources = [o["source"] for o in result.structured_output["options"]]
    assert "bigquery" not in sources
    assert "netsuite" in sources
    assert "shopify" in sources


@pytest.mark.asyncio
async def test_duplicate_option_ids_returns_error():
    """Two options with id='A' would render duplicate React keys client-side
    and the resume endpoint cannot disambiguate. Reject before persisting."""
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "A", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)
    assert "id" in result.error_message.lower()


@pytest.mark.asyncio
async def test_out_of_range_option_id_returns_error():
    """Option ids must be in {A, B, C}. Reject 'X'."""
    bad_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "X", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }
    result = await intercept_clarify_call(
        tool_input=bad_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptError)
    assert "id" in result.error_message.lower()


@pytest.mark.asyncio
async def test_valid_unique_ids_passes():
    """A/B/C unique ids → happy path."""
    valid_input = {
        "options": [
            {"id": "A", "title": "NetSuite", "rationale": "GL", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "BigQuery", "rationale": "BQ", "source": "bigquery", "is_default": False},
            {"id": "C", "title": "Shopify", "rationale": "Shop", "source": "shopify", "is_default": False},
        ],
        "ambiguity_summary": "Revenue can mean three things.",
    }
    result = await intercept_clarify_call(
        tool_input=valid_input,
        session_id="sess-1",
        active_connectors=["netsuite", "bigquery", "shopify"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult)


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


# ---------------------------------------------------------------------------
# Round 8 Bug 2 — REST-only Shopify/Stripe sources advertised but
# unfulfillable.
#
# Round 2 extended ``active_connectors`` to include REST ``connections``
# providers, but there are no local ``shopify_*`` / ``stripe_*`` chat tools
# in the registry. So a tenant with REST Stripe but no Stripe MCP gets
# ``source="stripe"`` advertised on the clarify card, picks it, and the
# resume-turn tool filter drops everything → stuck.
#
# Fix: constrain canonical-source map to providers that have local chat
# tools. REST shopify/stripe (provider == 'shopify' / 'stripe') are
# reconciliation-only — no chat tools — so they MUST NOT canonicalize.
# ---------------------------------------------------------------------------


def test_rest_shopify_provider_does_not_canonicalize():
    """REST Shopify (``connections.provider == 'shopify'``) has no local
    chat tools — only the MCP variant does. Must NOT canonicalize so
    clarify intercept doesn't advertise an unfulfillable source.
    """
    result = canonicalize_connector_providers(["shopify"])
    assert "shopify" not in result, (
        "REST shopify (connections.provider) has no chat tools — only "
        "shopify_mcp does. Canonicalizing it would let clarify intercept "
        "accept source='shopify' options that the resume turn cannot "
        "fulfill (the tool filter would strip everything). Round 8 Bug 2."
    )


def test_rest_stripe_provider_does_not_canonicalize():
    """REST Stripe (``connections.provider == 'stripe'``) is for
    reconciliation only — no chat tools. Must NOT canonicalize.
    """
    result = canonicalize_connector_providers(["stripe"])
    assert "stripe" not in result, (
        "REST stripe (connections.provider) is reconciliation-only and "
        "has no chat tools — only stripe_mcp does. Canonicalizing it "
        "would advertise an unfulfillable source. Round 8 Bug 2."
    )


def test_mcp_shopify_still_canonicalizes():
    """MCP Shopify still canonicalizes — it has chat tools via the
    ext__<uuid>__* tool-naming convention.
    """
    result = canonicalize_connector_providers(["shopify_mcp"])
    assert "shopify" in result


def test_mcp_stripe_still_canonicalizes():
    """MCP Stripe still canonicalizes — it has chat tools via ext__."""
    result = canonicalize_connector_providers(["stripe_mcp"])
    assert "stripe" in result


def test_rest_netsuite_still_canonicalizes():
    """REST NetSuite (``connections.provider == 'netsuite'``) DOES
    canonicalize because there are local netsuite_* chat tools (e.g.
    ``netsuite_suiteql``). Don't break the NetSuite path.
    """
    result = canonicalize_connector_providers(["netsuite"])
    assert "netsuite" in result
