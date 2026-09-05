"""Task 4 -- inventory gating for the four `celigo.*` chat tools
(spec `docs/superpowers/specs/2026-09-04-celigo-chat-access.md` §5-§6, task 4A).

`build_all_tool_definitions` must surface `celigo_integrations` / `celigo_flows` /
`celigo_flow_steps` / `celigo_flow_errors` iff BOTH:
  - the tenant's `celigo` feature flag is on, AND
  - the tenant has an active flow-map `connections` row (`provider == "celigo"`,
    resolved via `app.services.celigo.read_queries._get_celigo_connection`).

This is deliberately NOT keyed on the `celigo_mcp` external MCP connector --
Framework has the flow-map connection and no such connector, and
`_CONNECTOR_GATED_TOOLS` is left untouched (task brief §A) because its keys
double as the provider EXCLUSION list for external tool definitions; adding
`celigo_mcp` there would silently drop every external Celigo MCP tool.

Real DB, real tenant, real flag/connection rows -- build_all_tool_definitions
itself is under test, not a mock of it.
"""

from __future__ import annotations

import uuid

from app.core.encryption import encrypt_credentials, get_current_key_version
from app.models.mcp_connector import McpConnector
from app.services.celigo.client import CELIGO_MCP_SERVER_URL
from app.services.celigo_write_guard import celigo_writes_allowed
from app.services.chat.tools import build_all_tool_definitions
from tests.api.test_celigo_flows_api import _make_connection
from tests.conftest import create_test_tenant, enable_feature_flag

_CELIGO_LOCAL_NAMES = {"celigo_integrations", "celigo_flows", "celigo_flow_steps", "celigo_flow_errors"}


async def _seed_celigo_mcp_connector(db, tenant_id) -> McpConnector:
    """The EXTERNAL Celigo MCP connector -- a different thing from the
    `connections` row `_make_connection` inserts. Mirrors
    `tests/test_connections.py::_seed_celigo_mcp_connector`."""
    connector = McpConnector(
        tenant_id=tenant_id,
        provider="celigo_mcp",
        label="Celigo (agent access)",
        server_url=CELIGO_MCP_SERVER_URL,
        auth_type="bearer",
        encrypted_credentials=encrypt_credentials({"token": "agent-tok"}),
        encryption_key_version=get_current_key_version(),
        status="active",
        is_enabled=True,
        discovered_tools=[{"name": "list_flows", "description": "List flows"}],
    )
    with celigo_writes_allowed(db):
        db.add(connector)
        await db.flush()
    return connector


def _names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


class TestCeligoInventoryGating:
    async def test_flag_on_and_connection_present_shows_local_tools(self, db):
        tenant = await create_test_tenant(db, slug=f"celigo-inv-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        await _make_connection(db, tenant.id)

        tools = await build_all_tool_definitions(db, tenant.id)

        assert _CELIGO_LOCAL_NAMES <= _names(tools)

    async def test_external_celigo_mcp_tools_still_present_alongside_local_ones(self, db):
        """The gate this task adds is additive -- it must never suppress the
        pre-existing external `ext__` Celigo tools when a `celigo_mcp`
        connector is ALSO configured (both can legitimately coexist)."""
        tenant = await create_test_tenant(db, slug=f"celigo-inv-ext-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        await _make_connection(db, tenant.id)
        connector = await _seed_celigo_mcp_connector(db, tenant.id)

        tools = await build_all_tool_definitions(db, tenant.id)
        names = _names(tools)

        assert _CELIGO_LOCAL_NAMES <= names
        assert any(n.startswith(f"ext__{connector.id.hex}__") for n in names), (
            "external Celigo MCP tool missing from the inventory alongside the local celigo.* tools"
        )

    async def test_no_connection_hides_local_tools(self, db):
        tenant = await create_test_tenant(db, slug=f"celigo-inv-noconn-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        # No `_make_connection` call -- flag on, connection absent.

        tools = await build_all_tool_definitions(db, tenant.id)

        assert _names(tools).isdisjoint(_CELIGO_LOCAL_NAMES)

    async def test_flag_off_hides_local_tools_even_with_a_connection(self, db):
        tenant = await create_test_tenant(db, slug=f"celigo-inv-flagoff-{uuid.uuid4().hex[:6]}")
        await _make_connection(db, tenant.id)
        # celigo flag defaults off -- never enabled for this tenant.

        tools = await build_all_tool_definitions(db, tenant.id)

        assert _names(tools).isdisjoint(_CELIGO_LOCAL_NAMES)

    async def test_gate_lookup_raising_drops_tools_and_does_not_raise(self, db, monkeypatch):
        """Fail closed: if the connection lookup blows up (e.g. a transient DB
        error), the tools must simply be absent -- never propagate and never
        appear anyway."""
        tenant = await create_test_tenant(db, slug=f"celigo-inv-raise-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        await _make_connection(db, tenant.id)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr("app.services.celigo.read_queries._get_celigo_connection", _boom)

        tools = await build_all_tool_definitions(db, tenant.id)

        assert _names(tools).isdisjoint(_CELIGO_LOCAL_NAMES)

    async def test_other_local_tools_unaffected_by_the_celigo_gate(self, db):
        tenant = await create_test_tenant(db, slug=f"celigo-inv-other-{uuid.uuid4().hex[:6]}")
        # No celigo flag, no connection -- celigo tools must be gone, everything else present.

        tools = await build_all_tool_definitions(db, tenant.id)
        names = _names(tools)

        assert "netsuite_suiteql" in names
        assert "reference_previous_result" in names
        assert _names(tools).isdisjoint(_CELIGO_LOCAL_NAMES)
