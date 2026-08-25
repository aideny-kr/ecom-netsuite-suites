"""MAJOR 3 (T2 gate round 4, PR #202): the `celigo` flag must be a two-layer
control, exactly like the read-only write policy already is.

The flag was enforced in ONE place -- `get_active_connectors_for_tenant`, whose
docstring calls it "the kill switch for the whole Celigo surface". But that
function builds the tool INVENTORY. `_execute_external_tool` loads the connector
by id via `get_mcp_connector`, which has no flag check, so a tool_use block
emitted before an operator flipped the flag off (or any caller that does not
re-derive the inventory) still executed a Celigo READ against the live MCP
server after the kill switch was thrown.

That is the single-layer mistake `.claude/rules/agent-graph.md` #3 names: the
dispatcher is the choke point and has several callers, so filtering at
definition time leaves a hole any new caller can walk through. This PR already
fixed it for WRITES at the dispatcher; the flag needed the same treatment.

The cost constraint is real -- this is the hot path for every tool call in every
chat turn -- so the flag lookup must sit behind the provider check and never
fire for a non-Celigo tool.
"""

import json
import uuid

import pytest

from app.core.encryption import encrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services.celigo_write_guard import celigo_writes_allowed
from app.services.chat.tools import _execute_external_tool, _make_ext_tool_name, execute_tool_call
from tests.conftest import enable_feature_flag

_DISCOVERED = [{"name": "list_flows", "description": "List flows", "input_schema": {"type": "object"}}]


async def seed_celigo_mcp_connector(db, tenant_id) -> McpConnector:
    """Seeding a celigo_mcp row is itself a guarded write -- holding the window
    here is the guard working, not a workaround (see
    tests/test_celigo_write_guard_containment.py's allowlist)."""
    connector = McpConnector(
        tenant_id=tenant_id,
        provider="celigo_mcp",
        label="Celigo (agent access)",
        server_url="https://api.integrator.io/celigo-mcp",
        auth_type="bearer",
        encrypted_credentials=encrypt_credentials({"token": "agent-token"}),
        status="active",
        is_enabled=True,
        discovered_tools=_DISCOVERED,
    )
    with celigo_writes_allowed(db):
        db.add(connector)
        await db.commit()
    return connector


async def seed_plain_mcp_connector(db, tenant_id) -> McpConnector:
    connector = McpConnector(
        tenant_id=tenant_id,
        provider="netsuite_mcp",
        label="NetSuite MCP",
        server_url="https://acme.suitetalk.api.netsuite.com/services/mcp/v1/all",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials({"access_token": "t"}),
        status="active",
        is_enabled=True,
        discovered_tools=[{"name": "ns_runCustomSuiteQL", "description": "Run SuiteQL"}],
    )
    db.add(connector)
    await db.commit()
    return connector


@pytest.fixture
def mcp_spy(monkeypatch):
    """Records every call that would have reached a live MCP server."""
    calls: list[tuple] = []

    async def _call(connector, raw_tool_name, tool_input, db=None):
        calls.append((connector.provider, raw_tool_name))
        return {"data": [{"id": "f1"}]}

    monkeypatch.setattr("app.services.mcp_client_service.call_external_mcp_tool", _call, raising=False)
    return calls


@pytest.fixture
def flag_spy(monkeypatch):
    """Counts feature-flag lookups so the hot path can be asserted on."""
    from app.services import feature_flag_service

    lookups: list[str] = []
    real_is_enabled = feature_flag_service.is_enabled

    async def _counting(db, tenant_id, flag_key):
        lookups.append(flag_key)
        return await real_is_enabled(db, tenant_id, flag_key)

    monkeypatch.setattr(feature_flag_service, "is_enabled", _counting)
    return lookups


class TestFlagIsEnforcedAtTheDispatcher:
    async def test_a_celigo_read_is_refused_when_the_flag_is_off(self, db, tenant_a, mcp_spy):
        """The flag is OFF by default -- no enable_feature_flag call here."""
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)

        result = await _execute_external_tool(connector.id, "list_flows", {}, tenant_a.id, db)

        assert "error" in result, f"expected a refusal, got {result!r}"
        assert mcp_spy == [], "the kill switch must stop the call before it reaches the MCP server"

    async def test_the_same_read_succeeds_when_the_flag_is_on(self, db, tenant_a, mcp_spy):
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)
        await enable_feature_flag(db, tenant_a.id, "celigo")

        result = await _execute_external_tool(connector.id, "list_flows", {}, tenant_a.id, db)

        assert "error" not in result, result
        assert mcp_spy == [("celigo_mcp", "list_flows")]

    async def test_the_refusal_also_holds_through_execute_tool_call(self, db, tenant_a, mcp_spy):
        """The dispatcher's public entry point -- the one an already-emitted
        tool_use block arrives through."""
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)

        raw = await execute_tool_call(
            _make_ext_tool_name(connector.id, "list_flows"),
            {},
            tenant_a.id,
            None,
            "corr-1",
            db,
        )

        assert "error" in json.loads(raw)
        assert mcp_spy == []

    async def test_a_write_tool_is_still_refused_regardless_of_the_flag(self, db, tenant_a, mcp_spy):
        """The read-only policy is independent of the flag and must not be
        weakened by adding the flag check ahead of it."""
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)
        await enable_feature_flag(db, tenant_a.id, "celigo")

        result = await _execute_external_tool(connector.id, "delete_resource", {}, tenant_a.id, db)

        assert "read-only" in result["error"]
        assert mcp_spy == []

    async def test_an_unknown_connector_is_still_refused(self, db, tenant_a, mcp_spy):
        result = await _execute_external_tool(uuid.uuid4(), "list_flows", {}, tenant_a.id, db)

        assert "error" in result
        assert mcp_spy == []


class TestTheHotPathStaysCheap:
    async def test_a_non_celigo_tool_triggers_no_flag_query(self, db, tenant_a, mcp_spy, flag_spy):
        """`_execute_external_tool` runs for every external tool call in every
        chat turn. Short-circuit on the provider first; a NetSuite call must not
        pay for a Celigo flag lookup."""
        connector = await seed_plain_mcp_connector(db, tenant_a.id)

        result = await _execute_external_tool(
            connector.id, "ns_runCustomSuiteQL", {"query": "SELECT 1"}, tenant_a.id, db
        )

        assert "error" not in result, result
        assert mcp_spy == [("netsuite_mcp", "ns_runCustomSuiteQL")]
        assert flag_spy == [], f"a non-Celigo tool call must not query any feature flag, saw {flag_spy}"

    async def test_a_celigo_call_queries_the_flag_exactly_once(self, db, tenant_a, mcp_spy, flag_spy):
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)
        await enable_feature_flag(db, tenant_a.id, "celigo")

        await _execute_external_tool(connector.id, "list_flows", {}, tenant_a.id, db)

        assert flag_spy == ["celigo"]
