from datetime import timedelta
from uuid import uuid4

import pytest

from app.models.transaction_ops import TransactionFinding
from app.services.transaction_ops import period_review
from tests.test_transaction_review_slices import finish, review


async def add(db, actor, run, ref, status, when, amount="123456789012345.123456"):
    db.add(
        TransactionFinding(
            tenant_id=actor.tenant_id,
            run_id=run.id,
            order_reference=ref,
            report_json={
                "balance": {"status": status, "currency": "USD", "amounts": {"order_total": {"source": amount}}}
            },
            created_at=when,
            updated_at=when,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_results_deduplicate_daily_slices_keep_exact_amounts_and_filter_after_latest(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    await add(db, actor, root, "R123456789", "difference", root.created_at)
    await finish(db, root)
    child = await period_review.continue_review(db, actor.tenant_id, root.id)
    await add(db, actor, child, "R123456789", "matched", root.created_at + timedelta(seconds=1))
    await add(db, actor, child, "R123456788", "missing_in_netsuite", root.created_at)
    data = await period_review.review_results(db, actor.tenant_id, root.id, limit=1)
    assert data["summary"] == {"checked": 2, "matched": 1, "needs_review": 1, "not_verified": 0}
    assert data["total"] == 2 and data["has_next"]
    assert len(data["items"]) == 1
    filtered = await period_review.review_results(db, actor.tenant_id, root.id, status="matched")
    assert filtered["total"] == 1 and filtered["items"][0]["order_reference"] == "R123456789"
    assert filtered["items"][0]["balance"]["amounts"]["order_total"]["source"] == "123456789012345.123456"
    assert filtered["items"][0]["run_id"] == str(child.id)
    from app.services.transaction_ops.state_service import StateError

    with pytest.raises(StateError, match="not_found"):
        await period_review.review_results(db, uuid4(), root.id)


@pytest.mark.asyncio
async def test_review_results_do_not_include_unrelated_review_or_guess_empty_matches(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, root = await review(db, actor, monkeypatch)
    await finish(db, root)
    other = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="yesterday"),
        actor=actor,
    )
    await add(db, actor, other, "R123456789", "matched", other.created_at)
    result = await period_review.review_results(db, actor.tenant_id, root.id)
    assert result["summary"]["checked"] == 0 and result["items"] == []


@pytest.fixture(autouse=True)
async def routes(app):
    from app.api.v1.transaction_ops import router

    app.include_router(router, prefix="/api/v1")


async def test_results_http_keeps_auth_and_server_bounds(client, db, admin_user, monkeypatch):
    actor, headers = admin_user
    _, root = await review(db, actor, monkeypatch)
    url = f"/api/v1/transaction-ops/runs/{root.id}/review/findings"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url + "?limit=101", headers=headers)).status_code == 422
    assert (await client.get(url + "?status=approved", headers=headers)).status_code == 422
    result = await client.get(url, headers=headers)
    assert result.status_code == 200 and result.json()["summary"]["checked"] == 0
