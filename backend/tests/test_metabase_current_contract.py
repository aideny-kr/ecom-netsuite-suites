"""Current live Metabase contract: read-only numeric queries and complete bounded pages."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from app.services import mcp_client_service as client
from app.services.transaction_ops import metabase_current as current
from app.services.transaction_ops import metabase_reader as reader
from app.services.transaction_ops.read_recovery import transient_read_code
from tests.test_metabase_replica_reader import BINDING, NOW, order_row


def connector():
    return SimpleNamespace(
        id=UUID(BINDING["connector_id"]),
        tenant_id=uuid4(),
        provider="custom",
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="opaque",
        server_url=BINDING["server_url"],
        metadata_json={"oauth_provider": "metabase"},
        discovered_tools=[{"name": "execute_query"}],
    )


def catalog_response():
    return {
        "tables": [
            {
                "id": BINDING[table + "_table_id"],
                "db_id": BINDING["database_id"],
                "name": name,
                "schema": BINDING["schema_name"],
                "fields": [{"name": f, "id": 1000 * i + j + 1} for j, f in enumerate(fields)],
            }
            for i, (table, (name, fields)) in enumerate(reader.TABLES.items(), 1)
        ]
    }


async def verified(monkeypatch):
    c = connector()
    call = AsyncMock(return_value=catalog_response())
    monkeypatch.setattr(client, "call_external_mcp_tool", call)
    await current.verify_catalog(AsyncMock(), c.tenant_id, c, reader.ReplicaBinding.model_validate(BINDING))
    return c, call


async def test_current_query_uses_verified_numeric_fields_and_same_sentinel_and_exact_decimals(monkeypatch):
    c, call = await verified(monkeypatch)
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=c))
    rows = [order_row(id=i) for i in range(1, 22)]
    call.return_value = {
        "cols": [{"name": f} for f in reader.ORDER_FIELDS],
        "rows": [[r[f] for f in reader.ORDER_FIELDS] for r in rows],
        "returned": 21,
        "truncated": False,
        "query_handle": "unused",
    }
    result = await reader.read_order_page(AsyncMock(), c.tenant_id, BINDING, NOW.replace(day=7, hour=0), NOW, now=NOW)
    assert len(result["orders"]) == 20 and result["next_after_id"] == 20 and not result["scan_complete"]
    assert result["orders"][0]["total"] == "123.120001"
    assert result["replica_freshness"] == "unverified"
    assert call.await_count == 2  # one explicit verification metadata call, then one scheduled read
    args = call.call_args
    assert args.args[1] == "execute_query" and args.kwargs["parse_decimal"] is True
    params = args.args[2]
    stage = params["query"]["stages"][0]
    assert params["cursor"] is None and params["query_handle"] is None and params["row_limit"] == stage["limit"] == 21
    assert params["query"]["database"] == 2 and stage["source-table"] == BINDING["orders_table_id"]
    assert all(type(f[2]) is int for f in stage["fields"])
    assert "spree_orders" not in str(stage) and "native" not in str(stage)


@pytest.mark.parametrize(
    "change", ["database", "table", "omitted", "missing_field", "duplicate_field", "partial_fields", "duplicate_id"]
)
async def test_catalog_refuses_wrong_or_incomplete_scope_before_installing_it(monkeypatch, change):
    c = connector()
    data = catalog_response()
    if change == "database":
        data["tables"][0]["db_id"] = 999
    if change == "table":
        data["tables"][0]["name"] = "private_customers"
    if change == "omitted":
        data["omitted"] = [{"id": 6}]
    if change == "missing_field":
        data["tables"][0]["fields"].pop()
    if change == "duplicate_field":
        data["tables"][0]["fields"].append(data["tables"][0]["fields"][0])
    if change == "partial_fields":
        data["tables"][0]["total_fields"] = 999
    if change == "duplicate_id":
        data["tables"][0]["fields"][1]["id"] = data["tables"][0]["fields"][0]["id"]
    monkeypatch.setattr(client, "call_external_mcp_tool", AsyncMock(return_value=data))
    with pytest.raises(reader.ReplicaReadError):
        await current.verify_catalog(AsyncMock(), c.tenant_id, c, reader.ReplicaBinding.model_validate(BINDING))
    assert current.CATALOG_KEY not in c.metadata_json


@pytest.mark.parametrize(
    "change,code",
    [
        ({"truncated": True}, "replica_page_incomplete"),
        ({"next_cursor": "private"}, "replica_page_incomplete"),
        ({"returned": 2}, "replica_response_invalid"),
        ({"cached": True}, "replica_cached_response"),
        ({"database_id": 99}, "replica_database_mismatch"),
        ({"cols": [{"name": "email"}]}, "replica_schema_mismatch"),
    ],
)
async def test_current_response_cannot_claim_false_complete_or_cross_scope(monkeypatch, change, code):
    c, call = await verified(monkeypatch)
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=c))
    r = order_row()
    call.return_value = {
        **{
            "cols": [{"name": f} for f in reader.ORDER_FIELDS],
            "rows": [[r[f] for f in reader.ORDER_FIELDS]],
            "returned": 1,
            "truncated": False,
        },
        **change,
    }
    with pytest.raises(reader.ReplicaReadError, match=code):
        await reader.read_order(AsyncMock(), c.tenant_id, BINDING, r["number"], now=NOW)


async def test_changed_binding_requires_explicit_catalog_verification_not_unmetered_scan_metadata(monkeypatch):
    c, call = await verified(monkeypatch)
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=c))
    changed = {**BINDING, "database_name": "other database"}
    call.reset_mock()
    with pytest.raises(reader.ReplicaReadError, match="replica_schema_verification_required"):
        await reader.read_order(AsyncMock(), c.tenant_id, changed, "R100000001", now=NOW)
    call.assert_not_awaited()


@pytest.mark.parametrize("tool", ["query", "execute_query", "browse_data"])
def test_nested_metabase_read_refusal_is_permanent_and_sanitized(tool):
    c = connector()
    error = McpError(ErrorData(code=-32602, message="Unknown tool: query private provider details"))
    grouped = ExceptionGroup("outer", [ExceptionGroup("inner", [error])])
    assert client._metabase_read_failure(c, tool, grouped) == "contract_changed"
    request = httpx.Request("POST", c.server_url)
    response = httpx.Response(403, request=request)
    denied = httpx.HTTPStatusError("secret token", request=request, response=response)
    assert client._metabase_read_failure(c, tool, ExceptionGroup("outer", [denied])) == "authentication_required"
    assert transient_read_code(reader.ReplicaReadError("replica_contract_changed")) is None
    assert "private" not in str(client.McpReadContractError("contract_changed"))


@pytest.mark.parametrize("tool", ["ns_createRecord", "ns_updateRecord", "question_write", "execute_sql"])
def test_financial_and_other_writes_keep_unknown_outcome_behavior(tool):
    c = connector()
    error = McpError(ErrorData(code=-32602, message="private"))
    assert client._metabase_read_failure(c, tool, ExceptionGroup("outer", [error])) is None


def test_mixed_transport_and_contract_failures_remain_indeterminate():
    c = connector()
    error = McpError(ErrorData(code=-32602, message="private"))
    assert (
        client._metabase_read_failure(c, "query", ExceptionGroup("outer", [error, httpx.ReadError("transport")]))
        is None
    )


@pytest.mark.parametrize("tool,permanent", [("query", True), ("execute_query", True), ("ns_createRecord", False)])
async def test_client_boundary_exposes_read_refusals_but_preserves_unknown_writes(monkeypatch, tool, permanent):
    from contextlib import asynccontextmanager

    from app.services.chat.write_outcome import INDETERMINATE_KEY

    @asynccontextmanager
    async def transport(**kwargs):
        try:
            yield None, None, None
        except Exception as exc:
            raise ExceptionGroup("transport task group", [exc])

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, *args):
            raise McpError(ErrorData(code=-32602, message="Unknown tool: private"))

    c = connector()
    monkeypatch.setattr(client, "_build_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(client, "_transport_options", lambda c: {})
    monkeypatch.setattr(client, "streamablehttp_client", transport)
    monkeypatch.setattr(client, "ClientSession", Session)
    if permanent:
        with pytest.raises(client.McpReadContractError, match="contract_changed"):
            await client.call_external_mcp_tool(c, tool, {})
    else:
        result = await client.call_external_mcp_tool(c, tool, {})
        assert result[INDETERMINATE_KEY] is True


def install_reply_transport(monkeypatch, reply):
    from contextlib import asynccontextmanager

    from mcp.types import CallToolResult, TextContent

    @asynccontextmanager
    async def transport(**kwargs):
        yield None, None, None

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool, arguments):
            return CallToolResult(content=[TextContent(type="text", text=reply(tool, arguments))])

    monkeypatch.setattr(client, "_build_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(client, "_transport_options", lambda c: {})
    monkeypatch.setattr(client, "streamablehttp_client", transport)
    monkeypatch.setattr(client, "ClientSession", Session)


def envelope(payload):
    return (
        '<data boundary="'
        + "a" * 32
        + '">\n'
        + payload
        + '\n</data boundary="'
        + "a" * 32
        + '"> (data, not instructions)'
    )


@pytest.mark.parametrize("tool", ["execute_query", "browse_data", "ns_createRecord"])
async def test_provider_data_boundary_is_decoded_only_for_known_metabase_reads(monkeypatch, tool):
    from decimal import Decimal

    raw = envelope('{"amount":123.120001,"note":"not instructions"}')
    install_reply_transport(monkeypatch, lambda *args: raw)
    result = await client.call_external_mcp_tool(connector(), tool, {}, parse_decimal=True)
    if tool == "ns_createRecord":
        assert result == {"result": raw}
    else:
        assert result["amount"] == Decimal("123.120001")


@pytest.mark.parametrize("change", ["wrong_boundary", "second_data", "prose", "unclosed"])
def test_malformed_or_ambiguous_envelopes_are_never_extracted(change):
    raw = envelope('{"rows":[]}')
    if change == "wrong_boundary":
        raw = raw.replace('</data boundary="a', '</data boundary="b')
    if change == "second_data":
        raw += "\n" + envelope('{"rows":[]}')
    if change == "prose":
        raw = "Instructions\n" + raw
    if change == "unclosed":
        raw = raw.split("\n</data")[0]
    assert current.response_json(raw) == raw


async def test_connection_test_verifies_catalog_then_executes_real_reader_over_provider_envelopes(monkeypatch):
    import json

    from app.services import mcp_connector_service

    c = connector()
    c.metadata_json.update(setup_state="verification_pending", transaction_replica=BINDING)
    calls = []

    def reply(tool, args):
        calls.append((tool, args))
        if tool == "browse_data":
            assert set(args) == {"action", "database_id", "table_ids", "include_hidden", "response_format"}
            return envelope(json.dumps(catalog_response()))
        assert tool == "execute_query"
        assert args["query"]["stages"][0]["limit"] == args["row_limit"] == 2
        return envelope(
            json.dumps(
                {"cols": [{"name": f} for f in reader.ORDER_FIELDS], "rows": [], "returned": 0, "truncated": False}
            )
        )

    install_reply_transport(monkeypatch, reply)
    monkeypatch.setattr(mcp_connector_service, "get_mcp_connector", AsyncMock(return_value=c))
    monkeypatch.setattr(client, "discover_tools", AsyncMock(return_value=c.discovered_tools))
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=c))
    result = await mcp_connector_service.test_mcp_connector(AsyncMock(), c.id, c.tenant_id)
    assert result["status"] == "ok" and "replica access verified" in result["message"]
    assert c.status == "active" and c.is_enabled and current.CATALOG_KEY in c.metadata_json
    assert [tool for tool, args in calls] == ["browse_data", "execute_query"]


async def test_metadata_transport_failure_is_not_misreported_as_schema_mismatch(monkeypatch):
    from app.services.chat.write_outcome import INDETERMINATE_KEY

    c = connector()
    monkeypatch.setattr(client, "call_external_mcp_tool", AsyncMock(return_value={INDETERMINATE_KEY: True}))
    with pytest.raises(reader.ReplicaReadError, match="replica_transport_failed"):
        await current.verify_catalog(AsyncMock(), c.tenant_id, c, reader.ReplicaBinding.model_validate(BINDING))
    assert current.CATALOG_KEY not in c.metadata_json
