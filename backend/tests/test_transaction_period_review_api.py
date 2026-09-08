from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.api.v1.transaction_ops import router
from app.services.transaction_ops import period_review
from tests.conftest import enable_feature_flag
from tests.test_metabase_replica_reader import BINDING
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture(autouse=True)
async def routes(app):
    app.include_router(router, prefix="/api/v1")


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


async def test_review_queues_exact_calendar_cohort_and_idempotent_retry(client, db, admin_user, monkeypatch):
    actor, headers = admin_user
    c = await ready(db, actor, monkeypatch)
    body = {"period": "last_month", "evaluation_key": str(uuid4())}
    url = f"/api/v1/transaction-ops/configs/{c.id}/review"
    r = await client.post(url, json=body, headers=headers)
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert datetime.fromisoformat(data["params_json"]["window_start"]) == datetime(2026, 8, 1, 7, tzinfo=timezone.utc)
    assert datetime.fromisoformat(data["params_json"]["window_end"]) == datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    assert data["params_json"]["window_basis"] == "completed_at"
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == data["id"]


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
