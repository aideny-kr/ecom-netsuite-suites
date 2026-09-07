"""Authenticated connector lifecycle, verification and tenant regressions."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.encryption import decrypt_credentials
from app.models.connection import Connection


def payload(provider="solidus"):
    return {
        "provider": provider,
        "label": "Test connector",
        "credentials": {
            "base_url": "https://store.example/api/",
            "auth_type": "bearer",
            "token": "credential-must-stay-private",
            **({"api_profile": "solidus_rest"} if provider == "solidus" else {"test_path": "health"}),
        },
    }


@pytest.mark.parametrize("provider", ["solidus", "api"])
async def test_verified_create_encrypts_and_delete_disappears(client, db, admin_user, provider, monkeypatch):
    user, headers = admin_user
    upstream = AsyncMock(return_value={"orders": []})
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    response = await client.post("/api/v1/connections", headers=headers, json=payload(provider))
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["status"] == "active"
    assert "credential-must-stay-private" not in response.text
    connection = (await db.execute(select(Connection).where(Connection.id == row["id"]))).scalar_one()
    assert decrypt_credentials(connection.encrypted_credentials)["token"] == "credential-must-stay-private"
    assert upstream.await_count == 1
    assert (await client.delete(f"/api/v1/connections/{row['id']}", headers=headers)).status_code == 204
    assert row["id"] not in [item["id"] for item in (await client.get("/api/v1/connections", headers=headers)).json()]
    retest = await client.post(f"/api/v1/connections/{row['id']}/test", headers=headers)
    assert retest.json()["status"] == "error"
    assert upstream.await_count == 1  # revoked credentials are never used again


async def test_failed_verification_is_saved_as_error_and_retry_can_recover(client, admin_user, monkeypatch):
    from app.services.http_connector_service import ConnectorReadError

    _, headers = admin_user
    upstream = AsyncMock(side_effect=ConnectorReadError("authentication_failed"))
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    response = await client.post("/api/v1/connections", headers=headers, json=payload())
    assert response.status_code == 201
    assert response.json()["status"] == "error"
    upstream.side_effect = None
    upstream.return_value = {"orders": []}
    result = await client.post(f"/api/v1/connections/{response.json()['id']}/test", headers=headers)
    assert result.json()["status"] == "ok"


async def test_invalid_settings_never_echo_credentials_or_make_a_request(client, admin_user, monkeypatch):
    _, headers = admin_user
    upstream = AsyncMock()
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    data = payload()
    data["credentials"]["base_url"] = "https://credential-must-stay-private@127.0.0.1/"
    response = await client.post("/api/v1/connections", headers=headers, json=data)
    assert response.status_code == 422
    assert "credential-must-stay-private" not in response.text
    upstream.assert_not_called()


async def test_other_tenant_and_viewer_cannot_use_or_delete_credentials(
    client, admin_user, admin_user_b, readonly_user, monkeypatch
):
    _, headers = admin_user
    upstream = AsyncMock(return_value={"orders": []})
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    created = (await client.post("/api/v1/connections", headers=headers, json=payload())).json()
    foreign = admin_user_b[1]
    assert (await client.get("/api/v1/connections", headers=foreign)).json() == []
    assert (await client.delete(f"/api/v1/connections/{created['id']}", headers=foreign)).status_code == 404
    assert (await client.post(f"/api/v1/connections/{created['id']}/test", headers=foreign)).json()["status"] == "error"
    assert (await client.post("/api/v1/connections", headers=readonly_user[1], json=payload())).status_code == 403
    assert upstream.await_count == 1


async def test_custom_mcp_failed_discovery_never_reports_active_or_leaks_exception(client, admin_user, monkeypatch):
    _, headers = admin_user
    discovery = AsyncMock(side_effect=RuntimeError("SECRET server error"))
    monkeypatch.setattr("app.services.mcp_client_service.discover_tools", discovery)
    response = await client.post(
        "/api/v1/mcp-connectors",
        headers=headers,
        json={
            "provider": "custom",
            "label": "Test MCP",
            "server_url": "https://mcp.example/mcp",
            "auth_type": "bearer",
            "credentials": {"access_token": "SECRET"},
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "error"
    assert "SECRET" not in response.text
    identifier = response.json()["id"]
    test = await client.post(f"/api/v1/mcp-connectors/{identifier}/test", headers=headers)
    assert test.json()["status"] == "error"
    assert "SECRET" not in test.text
    discovery.side_effect = None
    discovery.return_value = [{"name": "read_orders"}]
    assert (await client.post(f"/api/v1/mcp-connectors/{identifier}/test", headers=headers)).json()["status"] == "ok"
    assert (await client.delete(f"/api/v1/mcp-connectors/{identifier}", headers=headers)).status_code == 204
    assert identifier not in [
        item["id"] for item in (await client.get("/api/v1/mcp-connectors", headers=headers)).json()
    ]


async def test_deleted_connections_release_plan_capacity_and_netsuite_is_exempt(client, db, admin_user, monkeypatch):
    from app.models.tenant import Tenant

    user, headers = admin_user
    tenant = await db.get(Tenant, user.tenant_id)
    tenant.plan = "free"
    await db.flush()
    monkeypatch.setattr("app.services.http_connector_service.read_json", AsyncMock(return_value={"orders": []}))
    first = await client.post("/api/v1/connections", headers=headers, json=payload())
    second = await client.post("/api/v1/connections", headers=headers, json=payload())
    assert first.status_code == second.status_code == 201
    assert (await client.post("/api/v1/connections", headers=headers, json=payload())).status_code == 403
    assert (
        await client.post(
            "/api/v1/connections",
            headers=headers,
            json={"provider": "netsuite", "label": "NS", "credentials": {"account_id": "123"}},
        )
    ).status_code == 201
    assert (await client.delete(f"/api/v1/connections/{first.json()['id']}", headers=headers)).status_code == 204
    assert (await client.post("/api/v1/connections", headers=headers, json=payload())).status_code == 201
