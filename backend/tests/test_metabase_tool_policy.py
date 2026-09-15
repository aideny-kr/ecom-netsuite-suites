import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.metabase_tool_policy import is_read_only_metabase_tool, requires_custom_tool_confirmation
from app.services.chat.mutation_guard import classify_connector_mutation
from app.services.chat.tools import _execute_external_tool


def connector(**changes):
    values = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        is_enabled=True,
        provider="custom",
        auth_type="oauth2",
        server_url="https://framework.metabaseapp.com/api/metabase-mcp",
        metadata_json={"oauth_provider": "metabase"},
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "name", ["search", "read_resource", "query", "construct_query", "execute_query", "execute_question"]
)
def test_native_cloud_read_catalog(name):
    assert is_read_only_metabase_tool(connector(), name)
    assert not requires_custom_tool_confirmation(connector(), name)


@pytest.mark.parametrize(
    "name",
    ["execute_sql", "construct_native_query", "create_question", "update_dashboard", "delete_database", "unknown"],
)
def test_other_tools_remain_guarded(name):
    assert not is_read_only_metabase_tool(connector(), name)
    assert requires_custom_tool_confirmation(connector(), name)


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_type": "bearer"},
        {"metadata_json": {}},
        {"provider": "shopify_mcp"},
        {"server_url": "https://evil.example/api/metabase-mcp"},
        {"server_url": "https://framework.metabaseapp.com.evil.example/api/metabase-mcp"},
        {"server_url": "https://framework.metabaseapp.com@evil.example/api/metabase-mcp"},
        {"server_url": "http://framework.metabaseapp.com/api/metabase-mcp"},
        {"server_url": "https://framework.metabaseapp.com:444/api/metabase-mcp"},
        {"server_url": "https://framework.metabaseapp.com/api/metabase-mcp?redirect=elsewhere"},
        {"server_url": "https://framework.metabaseapp.com/api/other"},
        {"server_url": "https://[invalid"},
    ],
)
def test_name_or_hint_cannot_promote_an_arbitrary_custom_server(changes):
    assert not is_read_only_metabase_tool(connector(**changes), "search")


@pytest.mark.parametrize(
    "name,expected", [("search", None), ("execute_sql", "execute"), ("create_question", "execute")]
)
async def test_agent_classification_uses_tenant_bound_connector(name, expected):
    conn = connector()
    db = AsyncMock()
    with patch("app.services.mcp_connector_service.get_mcp_connector", new=AsyncMock(return_value=conn)) as fetch:
        result = await classify_connector_mutation(f"ext__{conn.id.hex}__{name}", db, conn.tenant_id)
    assert result == expected
    fetch.assert_awaited_once_with(db, conn.id, conn.tenant_id)


@pytest.mark.parametrize(
    "name,approved,executes",
    [
        ("search", False, True),
        ("execute_sql", False, False),
        ("create_question", False, False),
        ("create_question", True, True),
    ],
)
async def test_dispatch_guard_is_independent_of_agent_interception(name, approved, executes):
    conn = connector()
    remote = AsyncMock(return_value={"success": True})
    with (
        patch("app.services.mcp_connector_service.get_mcp_connector", new=AsyncMock(return_value=conn)),
        patch("app.services.mcp_client_service.call_external_mcp_tool", new=remote),
    ):
        result = await _execute_external_tool(conn.id, name, {}, conn.tenant_id, AsyncMock(), human_approved=approved)
    assert bool(remote.await_count) is executes
    if not executes:
        assert result["hitl_required"] is True


async def test_missing_tenant_connector_never_calls_remote_service():
    remote = AsyncMock()
    with (
        patch("app.services.mcp_connector_service.get_mcp_connector", new=AsyncMock(return_value=None)),
        patch("app.services.mcp_client_service.call_external_mcp_tool", new=remote),
    ):
        result = await _execute_external_tool(uuid.uuid4(), "search", {}, uuid.uuid4(), AsyncMock())
    assert result["error"]
    remote.assert_not_awaited()


def test_custom_write_confirmation_signs_all_arguments_and_rejects_tampering():
    from copy import deepcopy

    from app.services.chat.write_confirmation_service import (
        build_confirmation_payload,
        validate_and_extract_confirmation,
    )

    name = f"ext__{uuid.uuid4().hex}__create_question"
    params = {"id": 10, "type": "question", "query": {"nested": [1, 2]}, "name": "Proposed question"}
    payload = build_confirmation_payload(
        mutation_type="execute",
        record_type="external tool create_question",
        tool_name=name,
        tool_input=params,
        session_id="source-test",
    )
    assert payload.proposed_fields == params
    assert validate_and_extract_confirmation(payload.model_dump(), "source-test") == (True, name, params)
    changed = deepcopy(payload.model_dump())
    changed["tool_input"]["query"]["nested"].append(3)
    assert validate_and_extract_confirmation(changed, "source-test")[0] is False
    assert validate_and_extract_confirmation(payload.model_dump(), "other-session")[0] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "DELETE FROM orders"},
        {"query": {"type": "native", "native": {"query": "DELETE FROM orders"}}},
        {
            "query": {
                "lib/type": "mbql/query",
                "stages": [{"lib/type": "mbql.stage/native", "native": "DELETE FROM orders"}],
            }
        },
        {
            "query": {
                "lib/type": "mbql/query",
                "stages": [{"lib/type": "mbql.stage/mbql", "joins": [{"stages": [{"native": "DELETE FROM orders"}]}]}],
            }
        },
    ],
)
@pytest.mark.parametrize("name", ["query", "construct_query", "execute_query", "execute_question"])
async def test_native_query_passthrough_never_reaches_metabase(name, payload):
    conn = connector()
    remote = AsyncMock()
    with (
        patch("app.services.mcp_connector_service.get_mcp_connector", new=AsyncMock(return_value=conn)),
        patch("app.services.mcp_client_service.call_external_mcp_tool", new=remote),
    ):
        result = await _execute_external_tool(conn.id, name, payload, conn.tenant_id, AsyncMock())
    assert "MBQL" in result["error"]
    assert not result.get("hitl_required")
    remote.assert_not_awaited()


async def test_structured_read_query_runs_without_a_confirmation():
    conn = connector()
    remote = AsyncMock(return_value={"status": "completed", "data": {"cols": [], "rows": []}})
    params = {
        "query": {
            "lib/type": "mbql/query",
            "stages": [{"lib/type": "mbql.stage/mbql", "source-table": ["db", "public", "orders"]}],
        }
    }
    with (
        patch("app.services.mcp_connector_service.get_mcp_connector", new=AsyncMock(return_value=conn)),
        patch("app.services.mcp_client_service.call_external_mcp_tool", new=remote),
    ):
        result = await _execute_external_tool(conn.id, "query", params, conn.tenant_id, AsyncMock())
    assert result["status"] == "completed"
    remote.assert_awaited_once()


def test_null_query_with_native_handle_is_not_mistaken_for_sql():
    from app.services.chat.metabase_tool_policy import metabase_query_input_error

    params = {"query": None, "query_handle": str(uuid.uuid4())}
    assert metabase_query_input_error(connector(), "execute_query", params) is None
    assert metabase_query_input_error(connector(), "query", params) is None
    assert metabase_query_input_error(connector(), "construct_query", params)


def test_native_direct_query_inventory_avoids_session_scoped_handles():
    from app.services.chat.tools import build_external_tool_definitions

    native = connector(
        discovered_tools=[
            {"name": name} for name in ["search", "query", "construct_query", "execute_query", "execute_question"]
        ]
    )
    names = {tool["name"].rsplit("__", 1)[-1] for tool in build_external_tool_definitions([native])}
    assert names == {"search", "query", "execute_question"}
    native.discovered_tools = [tool for tool in native.discovered_tools if tool["name"] != "query"]
    names = {tool["name"].rsplit("__", 1)[-1] for tool in build_external_tool_definitions([native])}
    assert "construct_query" in names and "execute_query" in names


def test_advertised_mbql_schema_rejects_sql_and_preserves_native_metadata():
    from copy import deepcopy

    from jsonschema import ValidationError, validate

    from app.services.chat.tools import build_external_tool_definitions

    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "object"},
            "query_handle": {"type": "string"},
            "continuation_token": {"type": "string"},
        },
    }
    original = deepcopy(schema)
    conn = connector(discovered_tools=[{"name": "query", "input_schema": schema}])
    advertised = build_external_tool_definitions([conn])[0]["input_schema"]
    validate(
        {
            "query": {
                "lib/type": "mbql/query",
                "stages": [{"lib/type": "mbql.stage/mbql", "source-table": ["db", "public", "orders"]}],
            }
        },
        advertised,
    )
    validate({"query": None, "query_handle": "opaque"}, advertised)
    for query in [
        "SELECT * FROM orders",
        {"type": "native", "native": {"query": "SELECT 1"}},
        {"lib/type": "mbql/query", "stages": []},
    ]:
        with pytest.raises(ValidationError):
            validate({"query": query}, advertised)
    assert advertised["properties"]["continuation_token"] == original["properties"]["continuation_token"]
    assert schema == original
    unrelated = connector(auth_type="bearer", discovered_tools=[{"name": "query", "input_schema": schema}])
    assert build_external_tool_definitions([unrelated])[0]["input_schema"] == original
