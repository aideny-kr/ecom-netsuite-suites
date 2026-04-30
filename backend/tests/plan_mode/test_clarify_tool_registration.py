"""Verify clarify tool appears in inventory IFF plan_mode_enabled is true."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_clarify_in_inventory_when_flag_on(monkeypatch):
    """When plan_mode_enabled=True, clarify is in build_all_tool_definitions output."""
    from app.services.chat.tools import build_all_tool_definitions

    # Patch get_active_connectors_for_tenant to return [] so we don't hit DB
    async def _no_connectors(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_active_connectors_for_tenant",
        _no_connectors,
    )

    tools = await build_all_tool_definitions(db=None, tenant_id=uuid.uuid4(), plan_mode_enabled=True)
    names = [t["name"] for t in tools]
    assert "clarify" in names


@pytest.mark.asyncio
async def test_clarify_absent_when_flag_off(monkeypatch):
    """Default (plan_mode_enabled=False) — clarify NOT in output."""
    from app.services.chat.tools import build_all_tool_definitions

    async def _no_connectors(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_active_connectors_for_tenant",
        _no_connectors,
    )

    tools = await build_all_tool_definitions(db=None, tenant_id=uuid.uuid4())
    names = [t["name"] for t in tools]
    assert "clarify" not in names


@pytest.mark.asyncio
async def test_clarify_absent_when_flag_explicit_false(monkeypatch):
    from app.services.chat.tools import build_all_tool_definitions

    async def _no_connectors(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_active_connectors_for_tenant",
        _no_connectors,
    )

    tools = await build_all_tool_definitions(db=None, tenant_id=uuid.uuid4(), plan_mode_enabled=False)
    names = [t["name"] for t in tools]
    assert "clarify" not in names
