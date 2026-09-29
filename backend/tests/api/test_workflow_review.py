"""Builder contracts use synthetic plans and never dispatch a provider call."""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.job import Job
from app.models.pipeline import Schedule


async def seed(db, user, plan=None):
    row = Schedule(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        name="Synthetic workflow",
        schedule_type="job",
        instruction="Reconcile saved orders",
        is_active=True,
        plan_status="pending_approval",
        plan_version=0,
        timezone="UTC",
        plan_json=plan or {"steps": [{"id": "recon", "type": "recon.run", "params": {"window_days": 7}}]},
    )
    db.add(row)
    await db.flush()
    return row


async def test_validation_never_executes_or_approves(client, db, admin_user, monkeypatch):
    user, headers = admin_user
    row = await seed(db, user)

    def forbidden(*args, **kwargs):
        raise AssertionError("validation dispatched work")

    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", forbidden)
    response = await client.post(f"/api/v1/schedules/{row.id}/validate", json={"use_pending": False}, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["structurally_valid"] is True
    assert len(data["plan_hash"]) == 64
    assert data["steps"][0]["label"] == "Run reconciliation"
    assert data["execution_verified"] is False
    await db.refresh(row)
    assert row.plan_status == "pending_approval"
    assert await db.scalar(select(func.count()).select_from(Job).where(Job.tenant_id == user.tenant_id)) == 0


async def test_unknown_step_cannot_be_approved(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user, {"steps": [{"id": "write", "type": "netsuite.post", "params": {}}]})
    response = await client.post(f"/api/v1/schedules/{row.id}/approve", headers=headers)
    assert response.status_code == 409
    await db.refresh(row)
    assert row.plan_status == "pending_approval"


async def test_approval_rejects_changed_reviewed_plan(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user)
    validation = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    assert validation.status_code == 200
    fingerprint = validation.json()["plan_hash"]
    row.plan_json = {"steps": [{"id": "recon", "type": "recon.run", "params": {"window_days": 30}}]}
    await db.flush()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/approve", json={"plan_hash": fingerprint}, headers=headers
    )
    assert response.status_code == 409
    assert row.plan_version == 0


async def test_approval_matches_exact_review_and_increments_once(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user)
    validation = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    assert validation.status_code == 200
    body = {"plan_hash": validation.json()["plan_hash"]}
    response = await client.post(f"/api/v1/schedules/{row.id}/approve", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["plan_version"] == 1
    replay = await client.post(f"/api/v1/schedules/{row.id}/approve", json=body, headers=headers)
    assert replay.status_code in (400, 409)


async def test_foreign_validation_does_not_disclose_plan(client, db, admin_user):
    user, headers = admin_user
    response = await client.post(f"/api/v1/schedules/{uuid.uuid4()}/validate", json={}, headers=headers)
    assert response.status_code == 404


async def test_history_does_not_promote_execution_to_verification(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user)
    job = Job(
        tenant_id=user.tenant_id,
        job_type="scheduled_job",
        status="completed",
        parameters={"schedule_id": str(row.id), "plan_version": 1},
        result_summary={"reason": "done", "outputs": {}},
    )
    db.add(job)
    await db.flush()
    response = await client.get(f"/api/v1/schedules/{row.id}/runs", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["verification"] == "not_verified"
    job.result_summary = {"reason": "done", "verification": "verified", "outputs": {}}
    await db.flush()
    response = await client.get(f"/api/v1/schedules/{row.id}/runs", headers=headers)
    assert response.json()[0]["verification"] == "verified"


@pytest.mark.parametrize(
    "field,value",
    [
        ("timezone", "America/Los_Angeles"),
        ("budget_json", {"seconds": 40}),
        ("delivery_json", {"drive": {"folder": "changed"}}),
        ("cron_expression", "0 8 * * *"),
    ],
)
async def test_settings_changes_invalidate_review(client, db, admin_user, field, value):
    user, headers = admin_user
    row = await seed(db, user)
    review = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    setattr(row, field, value)
    await db.flush()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/approve", json={"plan_hash": review.json()["plan_hash"]}, headers=headers
    )
    assert response.status_code == 409


async def test_pending_change_needs_its_own_review(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user)
    current = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    row.plan_status = "approved"
    row.pending_plan_json = row.plan_json
    await db.flush()
    stale = await client.post(
        f"/api/v1/schedules/{row.id}/approve", json={"plan_hash": current.json()["plan_hash"]}, headers=headers
    )
    assert stale.status_code == 409
    pending = await client.post(f"/api/v1/schedules/{row.id}/validate", json={"use_pending": True}, headers=headers)
    accepted = await client.post(
        f"/api/v1/schedules/{row.id}/approve", json={"plan_hash": pending.json()["plan_hash"]}, headers=headers
    )
    assert accepted.status_code == 200
    assert accepted.json()["pending_plan_json"] is None


async def test_foreign_schedule_validation_is_404(client, db, admin_user):
    from tests.conftest import create_test_tenant, create_test_user

    _, headers = admin_user
    other = await create_test_tenant(db)
    user, _ = await create_test_user(db, other)
    row = await seed(db, user)
    response = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    assert response.status_code == 404


async def test_viewer_cannot_validate_or_approve(client, db):
    from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

    tenant = await create_test_tenant(db)
    viewer, _ = await create_test_user(db, tenant, role_name="viewer")
    row = await seed(db, viewer)
    headers = make_auth_headers(viewer)
    for action in ("validate", "approve"):
        response = await client.post(f"/api/v1/schedules/{row.id}/{action}", json={}, headers=headers)
        assert response.status_code == 403


@pytest.mark.parametrize("change", ["plan", "catch_up"])
async def test_validation_rejects_stale_display_snapshot(client, db, admin_user, change):
    user, headers = admin_user
    row = await seed(db, user)
    displayed = (await client.get(f"/api/v1/schedules/{row.id}", headers=headers)).json()
    if change == "plan":
        row.plan_json = {"steps": [{"id": "recon", "type": "recon.run", "params": {"window_days": 30}}]}
    else:
        row.catch_up = "skip"
    await db.flush()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/validate", json={"expected_plan_hash": displayed.get("plan_hash")}, headers=headers
    )
    assert response.status_code == 409


async def test_null_budget_preserves_unlimited_compatibility(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user)
    row.budget_json = {"usd": None}
    await db.flush()
    response = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    assert response.json()["structurally_valid"] is True


async def test_empty_approval_body_keeps_legacy_compatibility(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user)
    response = await client.post(f"/api/v1/schedules/{row.id}/approve", json={}, headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize("invalid", ["usd", "principal", "inputs"])
async def test_agent_validation_blocks_unsupported_contract(client, db, admin_user, invalid):
    from tests.jobs.test_agent_step import request_data

    user, headers = admin_user
    params = {**request_data(), "principal_id": str(user.id), "tenant_id": str(user.tenant_id)}
    if invalid == "principal":
        params["principal_id"] = str(uuid.uuid4())
    elif invalid == "inputs":
        params["budget"]["output_tokens"] = 99999
    row = await seed(db, user, {"steps": [{"id": "review", "type": "agent.review_saved_case", "params": params}]})
    row.budget_json = {"usd": 1} if invalid == "usd" else {}
    await db.flush()
    response = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["structurally_valid"] is False
    assert response.json()["blockers"]
    assert (await client.post(f"/api/v1/schedules/{row.id}/approve", headers=headers)).status_code == 409


async def test_agent_validation_accepts_supported_limits(client, db, admin_user):
    from tests.jobs.test_agent_step import request_data

    user, headers = admin_user
    params = {**request_data(), "principal_id": str(user.id), "tenant_id": str(user.tenant_id)}
    row = await seed(db, user, {"steps": [{"id": "review", "type": "agent.review_saved_case", "params": params}]})
    row.budget_json = {"seconds": 120, "usd": None}
    await db.flush()
    response = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    assert response.json()["structurally_valid"] is True


async def test_compile_is_unlocked_and_intervening_edit_is_rejected(client, db, admin_user, monkeypatch):
    from app.api.v1 import schedules
    from tests.api.test_schedules_api import _compiled_plan

    user, headers = admin_user
    row = await seed(db, user)
    locked = []
    original_get = schedules._get_or_404

    async def observed_get(*args, **kwargs):
        locked.append(kwargs.get("lock", False))
        return await original_get(*args, **kwargs)

    async def concurrent_compile(*args, **kwargs):
        assert locked == [False], "row locked during provider compilation"
        row.catch_up = "skip"
        await db.flush()
        return _compiled_plan()

    monkeypatch.setattr(schedules, "_get_or_404", observed_get)
    monkeypatch.setattr(schedules, "compile_instruction", concurrent_compile)
    response = await client.patch(
        f"/api/v1/schedules/{row.id}", json={"instruction": "Changed instruction"}, headers=headers
    )
    assert response.status_code == 409
    assert locked == [False, True]
    assert row.instruction == "Reconcile saved orders"
