from datetime import timedelta
from uuid import uuid4

import pytest

from app.models.transaction_ops import TransactionFinding, TransactionRun
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


async def evidence(db, actor, run, ref, status, when, *, source_id="service-1", currency="USD"):
    row = TransactionFinding(
        tenant_id=actor.tenant_id,
        run_id=run.id,
        order_reference=ref,
        report_json={
            "source": {"record_id": source_id},
            "balance": {
                "status": status,
                "currency": currency,
                "amounts": {
                    "order_total": {"source": "0.00", "target": "0.00", "delta": "0.00"},
                    "tax": {"source": "0.00" if status == "matched" else "2.40"},
                    "refunds": {"source": "0.00"},
                },
            },
        },
        created_at=when,
        updated_at=when,
    )
    db.add(row)
    await db.flush()
    return row


async def recheck(db, actor, root, *, snapshot=None, config_id=None):
    row = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=config_id or root.config_id,
        config_snapshot=snapshot or root.config_snapshot,
        params_json={"order_references": ["R123456789"]},
        work_key=uuid4().hex,
        origin="manual",
        status="finished",
        termination_reason="done",
        max_api_calls=20,
        max_orders=1,
        deadline_at=root.deadline_at,
    )
    db.add(row)
    await db.flush()
    return row


async def test_later_rechecks_update_review_rows_filters_counts_and_links_without_rewriting_history(
    client, db, admin_user, monkeypatch
):
    actor, headers = admin_user
    _, root = await review(db, actor, monkeypatch)
    old = await evidence(db, actor, root, "R123456789", "difference", root.created_at)
    await evidence(db, actor, root, "R123456788", "matched", root.created_at, source_id="service-2")
    later = await recheck(db, actor, root)
    new = await evidence(db, actor, later, "R123456789", "matched", root.created_at + timedelta(seconds=1))
    await evidence(
        db, actor, later, "R123456788", "incomplete", root.created_at + timedelta(seconds=1), source_id="service-2"
    )
    # A recheck can refresh the cohort, but cannot add unrelated orders to it.
    await evidence(db, actor, later, "R999999999", "matched", root.created_at + timedelta(seconds=1))
    url = f"/api/v1/transaction-ops/runs/{root.id}/review/findings"
    result = (await client.get(url + "?status=matched&limit=1&search=R123456789", headers=headers)).json()
    assert result["summary"] == {"checked": 2, "matched": 1, "needs_review": 0, "not_verified": 1}
    assert result["total"] == 1 and result["has_next"] is False
    assert result["items"][0]["id"] == str(new.id)
    assert result["items"][0]["run_id"] == str(later.id)
    assert result["items"][0]["balance"]["amounts"]["tax"]["source"] == "0.00"
    assert (await client.get(url + "?status=needs_review", headers=headers)).json()["items"] == []
    historical = (await client.get(f"/api/v1/transaction-ops/runs/{root.id}/findings", headers=headers)).json()
    assert next(r for r in historical if r["id"] == str(old.id))["report_json"]["balance"]["status"] == "difference"


@pytest.mark.parametrize(
    "mismatch",
    [
        "source_connection_id",
        "source_step_id",
        "netsuite_account_id",
        "subsidiary_id",
        "record_type",
        "source_id",
        "currency",
        "missing_source_id",
        "older",
    ],
)
async def test_recheck_cannot_clear_a_different_identity_or_replace_newer_evidence(
    db, admin_user, monkeypatch, mismatch
):
    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    old = await evidence(db, actor, root, "R123456789", "difference", root.created_at)
    scope = dict(root.config_snapshot)
    if mismatch in scope:
        scope[mismatch] = "other-scope"
    later = await recheck(db, actor, root, snapshot=scope)
    await evidence(
        db,
        actor,
        later,
        "R123456789",
        "matched",
        root.created_at + timedelta(seconds=-1 if mismatch == "older" else 1),
        source_id=None
        if mismatch == "missing_source_id"
        else "other-record"
        if mismatch == "source_id"
        else "service-1",
        currency="CAD" if mismatch == "currency" else "USD",
    )
    result = await period_review.review_results(db, actor.tenant_id, root.id, status="needs_review")
    assert result["total"] == 1 and result["items"][0]["id"] == str(old.id)


async def test_rechecks_follow_business_scope_across_config_revisions_but_not_tenants(
    db, admin_user, admin_user_b, monkeypatch
):
    actor, other_actor = admin_user[0], admin_user_b[0]
    _, root = await review(db, actor, monkeypatch)
    other_config, _ = await review(db, other_actor, monkeypatch)
    new_config, _ = await review(db, actor, monkeypatch)
    old = await evidence(db, actor, root, "R123456789", "difference", root.created_at)
    foreign = await recheck(db, other_actor, root, config_id=other_config.id)
    await evidence(db, other_actor, foreign, "R123456789", "matched", root.created_at + timedelta(seconds=2))
    result = await period_review.review_results(db, actor.tenant_id, root.id)
    assert result["items"][0]["id"] == str(old.id)
    # A mapping revision and account spelling normalization retain business identity.
    snapshot = {**root.config_snapshot, "netsuite_account_id": "6738075-sb1", "mapping_json": {"revision": "new"}}
    later = await recheck(db, actor, root, snapshot=snapshot, config_id=new_config.id)
    new = await evidence(db, actor, later, "R123456789", "matched", root.created_at + timedelta(seconds=1))
    result = await period_review.review_results(db, actor.tenant_id, root.id, status="matched")
    assert result["total"] == 1 and result["items"][0]["id"] == str(new.id)
