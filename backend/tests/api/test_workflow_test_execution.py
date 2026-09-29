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
    assert row.last_run_status == "paused"
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


async def test_queued_modern_run_cannot_borrow_replacement_approval(client, db, admin_user, monkeypatch):
    from dataclasses import replace

    from app.services.jobs.readiness import execution_fingerprint, inspect_readiness
    from app.services.jobs.registry import STEP_REGISTRY
    from app.services.schedule_service import enqueue_run
    from app.workers.tasks.scheduled_jobs import run_schedule_now

    user, headers = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)

    async def approve():
        review = await inspect_readiness(db, row)
        row.plan_status = "approved"
        row.plan_version += 1
        row.parameters = {
            "workflow_review_required": True,
            "workflow_review": {
                "execution_hash": execution_fingerprint(row),
                "readiness_hash": review["readiness_hash"],
            },
        }
        await db.flush()

    await approve()
    job = await enqueue_run(db, schedule=row, tenant_id=user.tenant_id, actor_id=user.id, use_pending=False)
    await approve()

    async def forbidden(*a, **kw):
        raise AssertionError("executed superseded queued plan")

    monkeypatch.setitem(STEP_REGISTRY, "report.compose", replace(STEP_REGISTRY["report.compose"], executor=forbidden))
    outcome = await run_schedule_now(db, row.id, tenant_id=user.tenant_id, existing_job_id=job.id)
    assert outcome.reason == "blocked"
    assert row.paused_at is None


async def test_routine_verification_metadata_preserves_approval_but_account_change_does_not(db, admin_user):
    from app.core.encryption import encrypt_credentials
    from app.services.jobs.readiness import inspect_readiness

    user, _ = admin_user
    row = await seed(db, user, PLAN)
    await ready(db, user)
    source = await db.scalar(select(Connection).where(Connection.tenant_id == user.tenant_id))
    source.encrypted_credentials = encrypt_credentials(
        {"account_id": "original", "access_token": "old", "expires_at": 1}
    )
    await db.flush()
    before = await inspect_readiness(db, row)
    source.metadata_json = {"verification_at": "now", "accounting_profiles": {"unrelated": "change"}}
    source.encrypted_credentials = encrypt_credentials(
        {"account_id": "original", "access_token": "renewed", "expires_at": 2}
    )
    await db.flush()
    after = await inspect_readiness(db, row)
    assert before["readiness_hash"] == after["readiness_hash"]
    db.add(
        Connection(
            tenant_id=user.tenant_id,
            provider="netsuite",
            label="Inactive",
            status="disconnected",
            encrypted_credentials="unused",
        )
    )
    await db.flush()
    assert before["readiness_hash"] == (await inspect_readiness(db, row))["readiness_hash"]
    source.encrypted_credentials = encrypt_credentials(
        {"account_id": "different", "access_token": "renewed", "expires_at": 2}
    )
    await db.flush()
    assert before["readiness_hash"] != (await inspect_readiness(db, row))["readiness_hash"]


async def test_unapproved_paused_draft_can_resume_for_test(client, db, admin_user):
    user, headers = admin_user
    row = await seed(db, user, PLAN)
    row.parameters = {"workflow_review_required": True}
    await db.flush()
    assert (await client.post(f"/api/v1/schedules/{row.id}/pause", headers=headers)).status_code == 200
    assert (await client.post(f"/api/v1/schedules/{row.id}/resume", headers=headers)).status_code == 200
    assert row.plan_status == "pending_approval" and row.next_run_at is None


@pytest.mark.parametrize("key", ["subsidiary_id", "currency", "unsupported"])
async def test_statement_filters_cannot_be_silently_ignored(client, db, admin_user, key):
    import copy

    user, headers = admin_user
    plan = copy.deepcopy(PLAN)
    plan["steps"][0]["params"]["params"][key] = "123"
    row = await seed(db, user, plan)
    await ready(db, user)
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    assert not review["ready"] and any("unsupported report input" in b for b in review["blockers"])


@pytest.mark.parametrize("kind", ["report.render_pdf", "report.build_xlsx"])
async def test_synchronous_artifact_renderers_are_not_advertised_as_bounded_tests(db, admin_user, kind):
    from app.services.jobs.readiness import inspect_readiness

    user, _ = admin_user
    row = await seed(
        db, user, {"steps": [*PLAN["steps"], {"id": "artifact", "type": kind, "params": {"report_step": "report"}}]}
    )
    await ready(db, user)
    review = await inspect_readiness(db, row)
    assert review["ready"] and not review["test_supported"]


async def test_instruction_compilation_cannot_upgrade_legacy_draft_without_review(client, db, admin_user, monkeypatch):
    from app.services.jobs.compiler import CompiledPlan

    user, headers = admin_user
    row = await seed(db, user, PLAN)

    async def compile_plan(*a, **kw):
        return CompiledPlan(plan_json=PLAN, summary_line="Synthetic", kinds={"read"}, model="synthetic")

    monkeypatch.setattr("app.api.v1.schedules.compile_instruction", compile_plan)
    response = await client.patch(
        f"/api/v1/schedules/{row.id}", json={"instruction": "Compile new draft"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert (await client.post(f"/api/v1/schedules/{row.id}/approve", headers=headers)).status_code == 409


@pytest.mark.parametrize("change", ["replacement", "account", "policy"])
async def test_report_subreads_revalidate_actual_source_before_each_dispatch(
    client, db, admin_user, monkeypatch, change
):
    from types import SimpleNamespace

    from app.core.encryption import encrypt_credentials
    from app.mcp.tools import netsuite_suiteql
    from app.models.policy_profile import PolicyProfile
    from app.models.report import Report
    from app.services.chat import tools
    from app.workers.tasks.scheduled_jobs import run_schedule_now
    from tests.test_report_playbooks import _patch_executor, _trial_balance_by_params

    user, headers = admin_user
    tenant_id = user.tenant_id
    row = await seed(db, user, PLAN)
    await ready(db, user)
    source = await db.scalar(select(Connection).where(Connection.tenant_id == user.tenant_id))
    source.encrypted_credentials = encrypt_credentials({"account_id": "synthetic-first"})
    await db.flush()
    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", lambda *a, **k: None)
    review = (await client.post(f"/api/v1/schedules/{row.id}/validate", json={}, headers=headers)).json()
    queued = await client.post(
        f"/api/v1/schedules/{row.id}/test",
        json={"expected_plan_hash": review["plan_hash"], "readiness_hash": review["readiness_hash"]},
        headers=headers,
    )
    assert queued.status_code == 202, queued.text
    jid = uuid.UUID(queued.json()["jobs_id"])
    _patch_executor(monkeypatch, by_params=_trial_balance_by_params())
    fake = tools.execute_tool_call

    async def financial_transport(tool_name, tool_input, **kw):
        # Exercise the actual selector/credential guard. Only the outbound HTTP
        # response and statement rows are synthetic; no provider is contacted.
        result = await netsuite_suiteql.execute(
            {"query": "SELECT id FROM transaction", "limit": 1}, context={"db": kw["db"], "tenant_id": kw["tenant_id"]}
        )
        assert not result.get("error"), result
        return await fake(tool_name, tool_input, **kw)

    monkeypatch.setattr(tools, "execute_tool_call", financial_transport)
    monkeypatch.setattr(netsuite_suiteql, "build_oauth1_header", lambda *a: {})
    calls = []

    class HTTP:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            calls.append(url)
            assert len(calls) == 1, "second provider call escaped its reviewed source"
            if change == "replacement":
                source.status = "disconnected"
                db.add(
                    Connection(
                        tenant_id=user.tenant_id,
                        provider="netsuite",
                        label="Replacement",
                        status="active",
                        encrypted_credentials=encrypt_credentials({"account_id": "different"}),
                    )
                )
            elif change == "account":
                source.encrypted_credentials = encrypt_credentials({"account_id": "different"})
            else:
                db.add(
                    PolicyProfile(
                        tenant_id=user.tenant_id,
                        name="Revoked",
                        version=1,
                        is_active=True,
                        tool_allowlist=["unrelated"],
                    )
                )
            await db.commit()
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"items": []})

    monkeypatch.setattr(netsuite_suiteql.httpx, "AsyncClient", HTTP)
    outcome = await run_schedule_now(db, row.id, tenant_id=user.tenant_id, existing_job_id=jid)
    assert outcome.reason == "blocked", outcome
    assert len(calls) == 1
    assert not (await db.scalars(select(Report).where(Report.tenant_id == tenant_id))).all()


async def test_bigquery_selector_excludes_disabled_connectors(db, admin_user):
    from app.mcp.tools.bigquery_tools import _get_bigquery_connector
    from app.models.mcp_connector import McpConnector

    user, _ = admin_user
    db.add(
        McpConnector(
            tenant_id=user.tenant_id,
            provider="bigquery",
            label="Disabled",
            server_url="https://example.test",
            status="active",
            is_enabled=False,
        )
    )
    await db.flush()
    assert await _get_bigquery_connector({"db": db, "tenant_id": user.tenant_id}) is None


async def test_legacy_mcp_create_cannot_mint_unreviewed_job(db, admin_user):
    from app.mcp.tools.schedule_ops import execute_create

    user, _ = admin_user
    result = await execute_create(
        {"name": "Legacy job", "schedule_type": "job"},
        context={"db": db, "tenant_id": user.tenant_id, "actor_id": user.id},
    )
    assert result.get("error"), result
