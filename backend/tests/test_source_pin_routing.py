"""Integration test: session.source_pin overrides default routing."""

import pytest


@pytest.mark.asyncio
async def test_source_pin_bigquery_forces_bi_agent(monkeypatch):
    """When session.source_pin = 'bigquery', the orchestrator must pick bi-agent
    regardless of query heuristics."""
    from app.services.chat.orchestrator import _select_agent_for_pin  # helper we'll add

    selected = _select_agent_for_pin(source_pin="bigquery")
    assert selected == "bi-agent"


@pytest.mark.asyncio
async def test_source_pin_netsuite_forces_unified_agent():
    from app.services.chat.orchestrator import _select_agent_for_pin

    selected = _select_agent_for_pin(source_pin="netsuite")
    assert selected == "unified-agent"


@pytest.mark.asyncio
async def test_no_source_pin_returns_none():
    from app.services.chat.orchestrator import _select_agent_for_pin

    assert _select_agent_for_pin(source_pin=None) is None
