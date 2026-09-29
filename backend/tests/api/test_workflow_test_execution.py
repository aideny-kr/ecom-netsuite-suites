"""Synthetic regression contracts for separately identified workflow test runs."""

import uuid

import pytest
from sqlalchemy import select

from app.models.connection import Connection
from app.models.job import Job
from app.services.jobs.inspection import plan_fingerprint
from tests.api.test_workflow_review import seed

PLAN = {
    "steps": [
        {
            "id": "report",
            "type": "report.compose",
            "params": {"playbook_key": "trial_balance", "params": {"period": "Jun 2026"}, "mode": "period"},
        }
    ]
}


async def ready(db, user):
    db.add(
        Connection(
            tenant_id=user.tenant_id,
            provider="netsuite",
            label="Synthetic",
            status="active",
            encrypted_credentials="synthetic",
        )
    )
    await db.flush()


async def test_test_run_queues_unapproved_exact_snapshot_without_activation(client, db, admin_user, monkeypatch):
    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    sent = []
    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", lambda *a, **kw: sent.append((a, kw)))
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/test",
        json={"expected_plan_hash": review["plan_hash"], "readiness_hash": review.get("readiness_hash")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    job = await db.get(Job, uuid.UUID(response.json()["jobs_id"]))
    assert job.parameters["execution_mode"] == "test"
    assert job.parameters["plan"] == PLAN
    assert job.parameters["budget"]["seconds"] <= 60
    assert row.plan_status == "pending_approval" and row.plan_version == 0
    assert len(sent) == 1


@pytest.mark.parametrize(
    "plan",
    [
        {"steps": [*PLAN["steps"], {"id": "send", "type": "drive.upload", "params": {"report_step": "report"}}]},
        {"steps": [{"id": "report", "type": "report.compose", "params": {"report_id": str(uuid.uuid4())}}]},
        {"steps": [{"id": "query", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]},
    ],
)
async def test_unsupported_test_envelope_never_enqueues(client, db, admin_user, monkeypatch, plan):
    user, headers = admin_user
    row = await seed(db, user, plan)

    def forbidden(*a, **kw):
        raise AssertionError("unsafe dispatch")

    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", forbidden)
    response = await client.post(
        f"/api/v1/schedules/{row.id}/test",
        json={"expected_plan_hash": plan_fingerprint(row), "readiness_hash": "0" * 64},
        headers=headers,
    )
    assert response.status_code == 409
    assert not (await db.scalars(select(Job).where(Job.tenant_id == user.tenant_id))).all()


async def test_validation_exposes_missing_source_and_test_limits(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user, PLAN)
    response = await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)
    data = response.json()
    assert data["structurally_valid"] is True
    assert data["ready"] is False
    assert any("netsuite" in b.lower() for b in data["readiness_blockers"])
    assert data["test_supported"] is True
    assert data["test_seconds"] == 60


async def test_readiness_hash_changes_on_policy_and_permission_revocation(client, db, admin_user):
    from app.models.policy_profile import PolicyProfile

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    before = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    db.add(
        PolicyProfile(tenant_id=user.tenant_id, name="deny", version=1, is_active=True, tool_allowlist=["unrelated"])
    )
    await db.flush()
    after = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    assert before["readiness_hash"] != after["readiness_hash"]
    assert before["ready"] is True and after["ready"] is False


async def test_modern_approval_binds_readiness_and_blocks_revoked_context(client, db, admin_user):
    from app.models.policy_profile import PolicyProfile

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    db.add(
        PolicyProfile(tenant_id=user.tenant_id, name="deny", version=1, is_active=True, tool_allowlist=["unrelated"])
    )
    await db.flush()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/approve",
        json={"plan_hash": review["plan_hash"], "readiness_hash": review["readiness_hash"]},
        headers=headers,
    )
    assert response.status_code == 409
    assert row.plan_status == "pending_approval"


async def test_real_worker_test_mode_does_not_approve_activate_or_replay(client, db, admin_user, monkeypatch):
    from dataclasses import replace

    from app.services.jobs.registry import STEP_REGISTRY
    from app.workers.tasks.scheduled_jobs import run_schedule_now

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    sent = []
    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", lambda *a, **kw: sent.append(kw))
    calls = []

    async def report(ctx, params):
        calls.append(ctx.execution_mode)
        return {"report_id": str(uuid.uuid4()), "title": "Synthetic preview"}

    monkeypatch.setitem(STEP_REGISTRY, "report.compose", replace(STEP_REGISTRY["report.compose"], executor=report))
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/test",
        json={"expected_plan_hash": review["plan_hash"], "readiness_hash": review["readiness_hash"]},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["jobs_id"])
    result = await run_schedule_now(db, row.id, tenant_id=user.tenant_id, existing_job_id=job_id)
    assert result.reason == "done"
    await run_schedule_now(db, row.id, tenant_id=user.tenant_id, existing_job_id=job_id)
    assert calls == ["test"]
    await db.refresh(row)
    assert row.plan_status == "pending_approval" and row.last_run_at is None and row.next_run_at is None
    history = (await client.get(f"/api/v1/schedules/{row.id}/runs", headers=headers)).json()
    assert history[0]["execution_mode"] == "test" and history[0]["verification"] == "not_verified"


@pytest.mark.parametrize("change", ["policy", "permission", "source", "plan", "pause", "cancel", "interrupted"])
async def test_worker_rechecks_test_envelope_before_call(client, db, admin_user, monkeypatch, change):
    from dataclasses import replace

    from sqlalchemy import delete

    from app.models.policy_profile import PolicyProfile
    from app.models.user import UserRole
    from app.services.jobs.registry import STEP_REGISTRY
    from app.workers.tasks.scheduled_jobs import run_schedule_now

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", lambda *a, **kw: None)
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/test",
        json={"expected_plan_hash": review["plan_hash"], "readiness_hash": review["readiness_hash"]},
        headers=headers,
    )
    job = await db.get(Job, uuid.UUID(response.json()["jobs_id"]))
    if change == "policy":
        db.add(
            PolicyProfile(
                tenant_id=user.tenant_id, name="deny", version=1, is_active=True, tool_allowlist=["unrelated"]
            )
        )
    if change == "permission":
        await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
    if change == "source":
        source = await db.scalar(select(Connection).where(Connection.tenant_id == user.tenant_id))
        source.status = "disconnected"
    if change == "plan":
        row.plan_json = {"steps": [{"id": "r", "type": "recon.run", "params": {}}]}
    if change == "pause":
        from datetime import datetime, timezone

        row.paused_at = datetime.now(timezone.utc)
    if change == "cancel":
        job.status = "cancelled"
    if change == "interrupted":
        job.status = "running"
    await db.flush()

    async def forbidden(*a, **kw):
        raise AssertionError("executor called after revocation")

    monkeypatch.setitem(STEP_REGISTRY, "report.compose", replace(STEP_REGISTRY["report.compose"], executor=forbidden))
    result = await run_schedule_now(db, row.id, tenant_id=user.tenant_id, existing_job_id=job.id)
    assert result.reason == "blocked"


async def test_approved_readiness_is_rechecked_before_live_execution(client, db, admin_user, monkeypatch):
    from app.models.policy_profile import PolicyProfile
    from app.workers.tasks.scheduled_jobs import run_schedule_now

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    response = await client.post(
        f"/api/v1/schedules/{row.id}/approve",
        json={"plan_hash": review["plan_hash"], "readiness_hash": review["readiness_hash"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert row.parameters["workflow_review"]["readiness_hash"] == review["readiness_hash"]
    db.add(
        PolicyProfile(tenant_id=user.tenant_id, name="deny", version=1, is_active=True, tool_allowlist=["unrelated"])
    )
    await db.flush()
    result = await run_schedule_now(db, row.id, tenant_id=user.tenant_id)
    assert result.reason == "blocked"
    await db.refresh(row)
    assert row.paused_at is not None
    resume = await client.post(f"/api/v1/schedules/{row.id}/resume", headers=headers)
    assert resume.status_code == 409


async def test_reviewed_workflow_cannot_downgrade_to_legacy_approval(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user, PLAN)
    row.parameters = {"workflow_review_required": True}
    await db.flush()
    response = await client.post(f"/api/v1/schedules/{row.id}/approve", headers=headers)
    assert response.status_code == 409
    await ready(db, user)
    validation = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    missing_plan = await client.post(
        f"/api/v1/schedules/{row.id}/approve", json={"readiness_hash": validation["readiness_hash"]}, headers=headers
    )
    assert missing_plan.status_code == 409


async def test_test_report_is_inert_at_its_first_durable_insert(db, admin_user, monkeypatch):
    from app.services.report.playbooks import compose_playbook_report
    from tests.test_report_playbooks import _patch_executor, _trial_balance_by_params

    user, _ = admin_user
    _patch_executor(monkeypatch, by_params=_trial_balance_by_params())
    run_id = uuid.uuid4()
    report = await compose_playbook_report(
        db,
        playbook_key="trial_balance",
        params={"period": "Jun 2026"},
        tenant_id=user.tenant_id,
        actor_id=user.id,
        test_run_id=run_id,
    )
    assert report.auto_refresh == "off" and report.source_run_id == run_id
    assert report.title.startswith("Test ·")
    assert "Test ·" in report.rendered_html
    assert report.series_id is None and report.published_drive_url is None


async def test_readiness_requires_executor_source_not_unrelated_mcp(client, db, admin_user):
    from app.models.mcp_connector import McpConnector

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    db.add(
        McpConnector(
            tenant_id=user.tenant_id,
            provider="netsuite_mcp",
            label="Synthetic",
            server_url="https://example.test",
            status="active",
            is_enabled=True,
        )
    )
    await db.flush()
    first = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    assert not first["ready"]
    await ready(db, user)
    second = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    assert second["ready"] and len(second["source_bindings"]) == 1
    await ready(db, user)
    third = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    assert not third["ready"] and any("unambiguously" in b for b in third["readiness_blockers"])
