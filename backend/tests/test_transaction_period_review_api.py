from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from app.api.v1.transaction_ops import router
from app.services.transaction_ops import period_review, scheduler, state_service
from tests.conftest import enable_feature_flag
from tests.test_metabase_replica_reader import BINDING
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture(autouse=True)
async def routes(app):
    app.include_router(router, prefix="/api/v1")


@pytest.fixture(autouse=True)
def publisher(monkeypatch):
    publish = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "publish_investigation", publish)
    return publish


async def ready(db, actor, monkeypatch):
    from app.models.mcp_connector import McpConnector

    c = McpConnector(
        id=uuid4(),
        tenant_id=actor.tenant_id,
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
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    monkeypatch.setattr(period_review, "utc_now", lambda: datetime(2026, 9, 8, 18, tzinfo=timezone.utc))
    return await seed_config(
        db,
        actor.tenant_id,
        actor,
        mapping_json={
            "reference_field": "tranid",
            "currency_minor_units": {"USD": 2},
            "action_mode": "detect_only",
            "metabase_replica": {**BINDING, "connector_id": str(c.id)},
            "reconciliation_policy": {"timezone_name": "America/Los_Angeles"},
        },
    )


async def test_review_queues_exact_calendar_cohort_and_idempotent_retry(client, db, admin_user, monkeypatch, publisher):
    actor, headers = admin_user
    c = await ready(db, actor, monkeypatch)
    body = {"period": "last_month", "evaluation_key": str(uuid4())}
    url = f"/api/v1/transaction-ops/configs/{c.id}/review"
    r = await client.post(url, json=body, headers=headers)
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert datetime.fromisoformat(data["params_json"]["window_start"]) == datetime(2026, 8, 1, 7, tzinfo=timezone.utc)
    assert datetime.fromisoformat(data["params_json"]["window_end"]) == datetime(2026, 8, 2, 7, tzinfo=timezone.utc)
    assert datetime.fromisoformat(data["params_json"]["review"]["end"]) == datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    assert data["params_json"]["window_basis"] == "updated_at"
    publisher.assert_called_once_with(actor.tenant_id, UUID(data["id"]))
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == data["id"]
    assert publisher.call_count == 2  # Shared publisher deduplicates a retried pending run.


async def test_review_broker_failure_keeps_durable_run_for_recovery(client, db, admin_user, monkeypatch, publisher):
    actor, headers = admin_user
    config = await ready(db, actor, monkeypatch)
    publisher.side_effect = RuntimeError("broker unavailable")
    body = {"period": "yesterday", "evaluation_key": str(uuid4())}
    url = f"/api/v1/transaction-ops/configs/{config.id}/review"
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 202, response.text
    run = await state_service.get_run(db, actor.tenant_id, UUID(response.json()["id"]))
    assert run.status == "pending"
    assert run.api_calls_used == run.orders_used == 0
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == str(run.id)


async def test_review_terminal_retry_does_not_publish_or_restart(client, db, admin_user, monkeypatch, publisher):
    actor, headers = admin_user
    config = await ready(db, actor, monkeypatch)
    body = {"period": "yesterday", "evaluation_key": str(uuid4())}
    url = f"/api/v1/transaction-ops/configs/{config.id}/review"
    response = await client.post(url, json=body, headers=headers)
    run_id = UUID(response.json()["id"])
    token = await state_service.claim_run(db, actor.tenant_id, run_id)
    await state_service.finish_run(db, actor.tenant_id, run_id, "done", lease_token=token)
    publisher.reset_mock()
    retry = await client.post(url, json=body, headers=headers)
    assert retry.status_code == 202
    assert retry.json()["id"] == str(run_id)
    assert retry.json()["status"] == "finished"
    publisher.assert_not_called()


async def test_review_requires_authorized_tenant_before_publication(
    client, db, admin_user, admin_user_b, monkeypatch, publisher
):
    actor, headers = admin_user
    config = await ready(db, actor, monkeypatch)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, admin_user_b[0].tenant_id, flag)
    body = {"period": "yesterday", "evaluation_key": str(uuid4())}
    url = f"/api/v1/transaction-ops/configs/{config.id}/review"
    assert (await client.post(url, json=body)).status_code == 401
    assert (await client.post(url, json=body, headers=admin_user_b[1])).status_code == 404
    await enable_feature_flag(db, actor.tenant_id, "reconciliation", False)
    assert (await client.post(url, json=body, headers=headers)).status_code == 403
    publisher.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"period": "custom", "start_date": "2026-09-08", "end_date": "2026-09-08"},
        {"period": "last_month", "timezone_name": "UTC"},
        {"period": "last_month", "start_date": "2026-08-02"},
        {"period": "last_year"},
    ],
)
async def test_review_rejects_future_or_unbounded_or_caller_timezone(client, db, admin_user, monkeypatch, changes):
    actor, headers = admin_user
    c = await ready(db, actor, monkeypatch)
    response = await client.post(
        f"/api/v1/transaction-ops/configs/{c.id}/review",
        json={"evaluation_key": str(uuid4()), **changes},
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_ordinary_run_route_cannot_forge_period_coverage(client, db, admin_user, monkeypatch):
    actor, headers = admin_user
    c = await ready(db, actor, monkeypatch)
    response = await client.post(
        f"/api/v1/transaction-ops/configs/{c.id}/runs",
        headers=headers,
        json={
            "evaluation_key": "forged-review",
            "window_basis": "completed_at",
            "window_start": "2026-08-15T00:00:00Z",
            "window_end": "2026-08-16T00:00:00Z",
            "review": {"id": str(uuid4()), "start": "2026-08-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"},
        },
    )
    assert response.status_code == 422, response.text
