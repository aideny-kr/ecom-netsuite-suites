import uuid

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.pipeline import Schedule


async def test_health_and_usage_preserve_tenant_and_viewer_boundaries(
    client, db, admin_user, admin_user_b, readonly_user
):
    user, headers = admin_user
    other, other_headers = admin_user_b
    _, viewer_headers = readonly_user
    conn = Connection(
        tenant_id=user.tenant_id,
        provider="netsuite",
        label="Local ERP",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials({"account_id": "sandbox", "access_token": "never-return-me"}),
    )
    foreign = Connection(
        tenant_id=other.tenant_id,
        provider="netsuite",
        label="Foreign ERP",
        status="active",
        encrypted_credentials=encrypt_credentials({"token": "foreign-secret"}),
    )
    db.add_all([conn, foreign])
    await db.flush()
    schedule = Schedule(
        tenant_id=user.tenant_id,
        name="Bound workflow",
        schedule_type="sync",
        parameters={"connection_id": str(conn.id)},
        is_active=True,
    )
    foreign_schedule = Schedule(
        tenant_id=other.tenant_id,
        name="Foreign workflow",
        schedule_type="sync",
        parameters={"connection_id": str(conn.id)},
        is_active=True,
    )
    db.add_all([schedule, foreign_schedule])
    await db.flush()
    response = await client.get("/api/v1/connections/health", headers=headers)
    assert response.status_code == 200
    assert response.json()["connections"][0]["last_health_check"] is None
    assert "Foreign" not in response.text and "never-return-me" not in response.text
    response = await client.get(f"/api/v1/connections/usage/api/{conn.id}", headers=headers)
    assert response.status_code == 200
    assert [use["name"] for use in response.json()["uses"]] == ["Bound workflow"]
    response = await client.get(f"/api/v1/connections/usage/api/{conn.id}", headers=other_headers)
    assert response.status_code == 404
    response = await client.get(f"/api/v1/connections/usage/api/{conn.id}", headers=viewer_headers)
    assert response.status_code == 200
    assert response.json()["uses"] == []
    assert response.json()["visibility_limited"]
    response = await client.delete(f"/api/v1/connections/{conn.id}", headers=viewer_headers)
    assert response.status_code == 403
    await db.refresh(conn)
    assert conn.status == "active" and conn.last_health_check_at is None


async def test_usage_unknown_or_foreign_method_cannot_probe_dependencies(client, admin_user):
    _, headers = admin_user
    assert (await client.get(f"/api/v1/connections/usage/mcp/{uuid.uuid4()}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/connections/usage/invalid/{uuid.uuid4()}", headers=headers)).status_code == 422
