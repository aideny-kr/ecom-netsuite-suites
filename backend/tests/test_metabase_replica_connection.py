from unittest.mock import AsyncMock, create_autospec

import pytest

from app.models.mcp_connector import McpConnector
from app.services import mcp_connector_service
from app.services.transaction_ops import metabase_reader
from tests.test_metabase_replica_reader import BINDING


@pytest.mark.asyncio
@pytest.mark.parametrize("query_failed", [False, True])
async def test_bound_connection_test_verifies_the_same_replica_reader(db, admin_user, monkeypatch, query_failed):
    user = admin_user[0]
    c = McpConnector(
        tenant_id=user.tenant_id,
        provider="custom",
        label="Metabase",
        server_url=BINDING["server_url"],
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="opaque",
    )
    db.add(c)
    await db.flush()
    c.metadata_json = {"setup_state": "connected", "transaction_replica": {**BINDING, "connector_id": str(c.id)}}
    await db.flush()
    discovery = AsyncMock(return_value=[{"name": "query"}])
    query = create_autospec(metabase_reader.read_order_page, return_value={"orders": [], "page_complete": True})
    if query_failed:
        query.side_effect = metabase_reader.ReplicaReadError("replica_evidence_incomplete")
    monkeypatch.setattr("app.services.mcp_client_service.discover_tools", discovery)
    monkeypatch.setattr(metabase_reader, "read_order_page", query)
    result = await mcp_connector_service.test_mcp_connector(db, c.id, user.tenant_id)
    query.assert_awaited_once()
    assert query.call_args.args[1] == user.tenant_id
    assert result["status"] == ("error" if query_failed else "ok")
    if not query_failed:
        assert "replica" in result["message"].lower()
