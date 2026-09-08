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
