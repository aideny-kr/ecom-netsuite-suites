from uuid import uuid4

import pytest

from app.api.v1.transaction_ops import router
from app.services.transaction_ops import case_service
from tests.conftest import enable_feature_flag
from tests.test_transaction_cases import NOW, observe, report
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture(autouse=True)
async def routes(app):
    app.include_router(router, prefix="/api/v1")


async def test_authenticated_case_history_and_human_reinvestigation(client, db, admin_user):
    actor, headers = admin_user
    for feature in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, feature)
    config = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, config, report(), NOW)
    c = (await case_service.list_cases(db, actor.tenant_id))[0]
    prefix = "/api/v1/transaction-ops/cases"
    response = await client.get(prefix, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == str(c.id)
    history = await client.get(f"{prefix}/{c.id}/observations", headers=headers)
    assert history.status_code == 200 and len(history.json()) == 1
    body = {"evaluation_key": str(uuid4())}
    result = await client.post(f"{prefix}/{c.id}/investigate", json=body, headers=headers)
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "pending" and result.json()["params_json"]["order_references"] == [
        c.order_reference
    ]
    repeated = await client.post(f"{prefix}/{c.id}/investigate", json=body, headers=headers)
    assert repeated.json()["id"] == result.json()["id"]
    assert (await client.get(prefix)).status_code == 401


async def test_group_routes_require_auth_and_keep_members_tenant_scoped(client, db, admin_user, admin_user_b):
    from tests.test_transaction_case_groups import seed

    actor, headers = admin_user
    other, other_headers = admin_user_b
    for user in (actor, other):
        for feature in ("celigo", "reconciliation"):
            await enable_feature_flag(db, user.tenant_id, feature)
    await seed(db, actor.tenant_id, 2)
    prefix = "/api/v1/transaction-ops/case-groups"
    assert (await client.get(prefix)).status_code == 401
    response = await client.get(prefix, headers=headers)
    assert response.status_code == 200, response.text
    group = response.json()["groups"][0]
    assert group["case_count"] == 2
    members = await client.get(f"{prefix}/{group['group_id']}/cases?limit=1", headers=headers)
    assert members.status_code == 200 and members.json()["has_next"] is True
    assert len(members.json()["cases"]) == 1
    foreign = await client.get(f"{prefix}/{group['group_id']}/cases", headers=other_headers)
    assert foreign.status_code == 200 and foreign.json()["cases"] == []
    assert (await client.get(f"{prefix}/invalid/cases", headers=headers)).status_code == 422
    assert (await client.get(f"{prefix}?limit=501", headers=headers)).status_code == 422


async def test_group_period_query_validates_scope_and_never_exposes_foreign_reviews(
    client, db, admin_user, admin_user_b, monkeypatch
):
    from tests.test_transaction_review_slices import review

    actor, headers = admin_user
    _, root = await review(db, actor, monkeypatch)
    prefix = "/api/v1/transaction-ops/case-groups"
    params = {"review_run_ids": str(root.id), "status": "needs_review"}
    result = await client.get(prefix, params=params, headers=headers)
    assert result.status_code == 200 and result.json()["groups"] == [], result.text
    for feature in ("celigo", "reconciliation"):
        await enable_feature_flag(db, admin_user_b[0].tenant_id, feature)
    assert (await client.get(prefix, params=params, headers=admin_user_b[1])).status_code == 404
    assert (await client.get(prefix, params={"review_run_ids": "bad"}, headers=headers)).status_code == 422
    assert (await client.get(prefix, params={"status": "needs_review"}, headers=headers)).status_code == 422
    members = await client.get(f"{prefix}/{'a' * 32}/cases", params=params, headers=headers)
    assert members.status_code == 200 and members.json()["cases"] == []
