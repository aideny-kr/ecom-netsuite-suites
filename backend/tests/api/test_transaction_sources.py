"""Seeded-tenant authorization for the real read-only source endpoint."""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.api.v1.transaction_sources import router
from app.core.encryption import encrypt_credentials
from app.models.celigo import CeligoFlow, CeligoFlowStep, CeligoIntegration
from tests.conftest import create_test_user, enable_feature_flag, make_auth_headers

REMOTE_CONNECTION = "609c54e8a9a34b7255fa2ee9"
ORDER = "R123456789"


@pytest.fixture(autouse=True)
async def source_routes(app):
    # Parent integration owns the central router; this tests the actual router
    # in isolation before that integration commit, without weakening dependencies.
    app.include_router(router, prefix="/api/v1")


@pytest.fixture
def upstream(monkeypatch):
    async def request(method, path, **kwargs):
        if method == "GET":
            return {
                "_id": REMOTE_CONNECTION,
                "type": "http",
                "http": {"baseURI": "https://private-direct-access.frame.work/api/"},
            }
        if kwargs["body"]["http"]["relativeURI"].startswith("sync/"):
            return {
                "data": [
                    {
                        "orders": [{"number": ORDER, "total": "10.01", "currency": "EUR"}],
                        "current_page": 1,
                        "pages": 1,
                        "total_count": 1,
                        "per_page": 20,
                        "count": 1,
                    }
                ]
            }
        return {"data": [{"number": ORDER, "total": "10.01", "currency": "EUR", "token": "secret"}]}

    mock = AsyncMock(side_effect=request)
    monkeypatch.setattr("app.services.transaction_ops.source_reader._Transport.request", mock)
    return mock


async def seed_source(db, tenant_id, *, provider="celigo", status="active", connection_tenant=None, flow_tenant=None):
    connection_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant, :provider, 'Source evidence', :status, :credentials, 1)"
        ),
        {
            "id": connection_id,
            "tenant": connection_tenant or tenant_id,
            "provider": provider,
            "status": status,
            "credentials": encrypt_credentials({"token": "celigo-secret"}),
        },
    )
    integration = CeligoIntegration(
        tenant_id=flow_tenant or tenant_id,
        celigo_connection_id=connection_id,
        celigo_id=uuid.uuid4().hex[:24],
        name="Orders",
        raw_json={},
    )
    db.add(integration)
    await db.flush()
    flow = CeligoFlow(
        tenant_id=flow_tenant or tenant_id,
        celigo_connection_id=connection_id,
        integration_id=integration.id,
        celigo_id=uuid.uuid4().hex[:24],
        name="Orders",
        raw_json={},
    )
    db.add(flow)
    await db.flush()
    step = CeligoFlowStep(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        flow_id=flow.id,
        celigo_id=uuid.uuid4().hex[:24],
        role="generator",
        connection_celigo_id=REMOTE_CONNECTION,
        raw_json={},
    )
    db.add(step)
    await db.flush()
    return step


@pytest.mark.parametrize("suffix", [f"/orders/{ORDER}", "/orders?updated_since=2026-09-01T00:00:00Z"])
async def test_authorized_source_evidence(client, db, admin_user, upstream, suffix):
    user, headers = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")
    step = await seed_source(db, user.tenant_id)
    response = await client.get(f"/api/v1/transaction-sources/celigo/steps/{step.id}{suffix}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["orders"][0]["total"] == "10.01"
    assert "secret" not in response.text
    assert upstream.await_count == 2


async def test_flag_off_blocks_before_source(client, db, admin_user, upstream):
    user, headers = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo", False)
    step = await seed_source(db, user.tenant_id)
    response = await client.get(f"/api/v1/transaction-sources/celigo/steps/{step.id}/orders/{ORDER}", headers=headers)
    assert response.status_code == 403
    upstream.assert_not_awaited()


async def test_permission_required_even_when_flag_enabled(client, db, tenant_a, upstream):
    user, _ = await create_test_user(db, tenant_a, role_name="nonexistent-role")
    await enable_feature_flag(db, tenant_a.id, "celigo")
    step = await seed_source(db, tenant_a.id)
    response = await client.get(
        f"/api/v1/transaction-sources/celigo/steps/{step.id}/orders/{ORDER}", headers=make_auth_headers(user)
    )
    assert response.status_code == 403
    upstream.assert_not_awaited()


@pytest.mark.parametrize("scope", ["step", "connection", "flow"])
async def test_cross_tenant_never_reaches_upstream(client, db, admin_user, tenant_b, upstream, scope):
    user, headers = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")
    step = await seed_source(
        db,
        tenant_b.id if scope == "step" else user.tenant_id,
        connection_tenant=tenant_b.id if scope == "connection" else None,
        flow_tenant=tenant_b.id if scope == "flow" else None,
    )
    response = await client.get(f"/api/v1/transaction-sources/celigo/steps/{step.id}/orders/{ORDER}", headers=headers)
    assert response.status_code == 404
    upstream.assert_not_awaited()


@pytest.mark.parametrize("kwargs", [{"provider": "netsuite"}, {"status": "revoked"}, {"status": "superseded"}])
async def test_inactive_or_wrong_provider_is_unavailable(client, db, admin_user, upstream, kwargs):
    user, headers = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")
    step = await seed_source(db, user.tenant_id, **kwargs)
    response = await client.get(f"/api/v1/transaction-sources/celigo/steps/{step.id}/orders/{ORDER}", headers=headers)
    assert response.status_code == 404
    upstream.assert_not_awaited()


@pytest.mark.parametrize(
    "suffix", ["/orders/bad", "/orders?updated_since=bad", "/orders?updated_since=2026-09-01T00:00:00Z&page_size=21"]
)
async def test_api_request_validation(client, db, admin_user, upstream, suffix):
    user, headers = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")
    step = await seed_source(db, user.tenant_id)
    response = await client.get(f"/api/v1/transaction-sources/celigo/steps/{step.id}{suffix}", headers=headers)
    assert response.status_code == 422
    upstream.assert_not_awaited()
