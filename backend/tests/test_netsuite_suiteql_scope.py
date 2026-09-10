"""Explicit accounting reads cannot drift to another tenant/connection/environment."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.mcp.tools import netsuite_suiteql as tool
from app.models.connection import Connection


async def test_bound_query_checks_exact_connection_and_environment_before_network(
    db, admin_user, admin_user_b, monkeypatch
):
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.encrypted_credentials = "bound-credential"
    other = Connection(
        id=uuid4(),
        tenant_id=actor.tenant_id,
        provider="netsuite",
        label="Other account",
        status="active",
        encrypted_credentials="other-credential",
        encryption_key_version=1,
    )
    db.add(other)
    await db.flush()
    monkeypatch.setattr(
        tool,
        "decrypt_credentials",
        lambda value: {
            "account_id": "123_SB1" if value == "bound-credential" else "987",
            "auth_type": "oauth2",
        },
    )
    token = AsyncMock(return_value="test-token")
    network = AsyncMock(return_value={"columns": ["id"], "rows": [["200"]], "row_count": 1})
    monkeypatch.setattr("app.services.netsuite_oauth_service.get_valid_token", token)
    monkeypatch.setattr("app.services.netsuite_client.execute_suiteql", network)
    monkeypatch.setattr(tool, "_maybe_judge", AsyncMock(side_effect=lambda result, *args, **kw: result))
    context = {"tenant_id": actor.tenant_id, "db": db}
    params = {
        "query": "SELECT id FROM transaction WHERE id = 200 AND subsidiary = 1",
        "limit": 1,
        "connection_id": str(connection.id),
        "expected_account_id": "123-sb1",
    }
    result = await tool.execute(params, context)
    assert result["verified_connection_scope"] == {"connection_id": str(connection.id), "account_id": "123-sb1"}
    assert token.call_args.args[1].id == connection.id
    assert network.call_args.args[1] == "123-sb1"
    network.reset_mock()
    token.reset_mock()
    for changes in ({"expected_account_id": "123"}, {"connection_id": str(other.id)}, {"connection_id": str(uuid4())}):
        result = await tool.execute({**params, **changes}, context)
        assert result["error"] is True
        assert "verified_connection_scope" not in result
    foreign = await tool.execute(params, {"tenant_id": admin_user_b[0].tenant_id, "db": db})
    assert foreign["error"] is True
    connection.status = "inactive"
    await db.flush()
    assert (await tool.execute(params, context))["error"] is True
    network.assert_not_awaited()
    token.assert_not_awaited()


@pytest.mark.parametrize(
    "scope",
    [
        {"connection_id": str(uuid4())},
        {"expected_account_id": "123"},
        {"connection_id": "bad", "expected_account_id": "123"},
        {"connection_id": str(uuid4()), "expected_account_id": None},
        {"connection_id": str(uuid4()), "expected_account_id": "123/path"},
    ],
)
async def test_partial_or_invalid_scope_is_rejected_without_a_default_query(scope):
    db = AsyncMock()
    result = await tool.execute({"query": "SELECT 1", **scope}, {"db": db, "tenant_id": uuid4()})
    assert result["error"] is True
    db.execute.assert_not_awaited()


def test_scoped_parameters_are_exposed_to_agent():
    from app.services.chat.tools import build_local_tool_definitions

    schema = next(t for t in build_local_tool_definitions() if t["name"] == "netsuite_suiteql")["input_schema"]
    assert {"connection_id", "expected_account_id"} <= schema["properties"].keys()
