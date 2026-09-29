"""Real runtime role/commits, actual compiler and report pipeline, synthetic source transport."""

import asyncio
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import get_db, set_tenant_context
from app.main import create_app
from app.models.connection import Connection
from app.models.report import Report
from app.models.user import User
from app.services.jobs.compiler import CompilerLLM, compile_instruction
from app.workers.tasks.scheduled_jobs import run_schedule_now
from tests.api.test_workflow_test_execution import PLAN
from tests.conftest import make_auth_headers
from tests.jobs.test_compiler import FakeAdapter, _compile_plan_response
from tests.test_dedicated_runtime import installation  # noqa: F401
from tests.test_report_playbooks import _patch_executor, _trial_balance_by_params


async def test_restricted_runtime_build_test_approve_pause_and_real_edit_race(installation, monkeypatch):  # noqa: F811
    i = installation
    tenant_id = i["company"]
    factory = async_sessionmaker(i["engine"], expire_on_commit=False)
    async with factory() as db:
        await set_tenant_context(db, str(tenant_id))
        user = await db.scalar(select(User).where(User.tenant_id == tenant_id))
        headers = make_auth_headers(user)
        db.add(
            Connection(
                tenant_id=tenant_id,
                provider="netsuite",
                label="Synthetic",
                status="active",
                encrypted_credentials="synthetic",
            )
        )
        await db.commit()
    application = create_app()

    async def session():
        async with factory() as db:
            await set_tenant_context(db, str(tenant_id))
            yield db

    application.dependency_overrides[get_db] = session

    async def compile_synthetic(db, **kwargs):
        return await compile_instruction(
            db,
            **kwargs,
            llm=CompilerLLM(adapter=FakeAdapter([_compile_plan_response(PLAN)]), model="synthetic-compiler"),
        )

    monkeypatch.setattr("app.services.jobs.compiler.compile_instruction", compile_synthetic)
    monkeypatch.setattr("app.services.schedule_service.compile_instruction", compile_synthetic)
    monkeypatch.setattr("app.api.v1.schedules.compile_instruction", compile_synthetic)
    sent = []
    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", lambda *a, **kw: sent.append(kw))
    calls = _patch_executor(monkeypatch, by_params=_trial_balance_by_params())
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/schedules", json={"instruction": "Trial balance for Jun 2026"}, headers=headers
        )
        assert created.status_code == 201, created.text
        sid = uuid.UUID(created.json()["id"])
        path = f"/api/v1/schedules/{sid}"
        validation = (await client.post(path + "/validate", json={}, headers=headers)).json()
        assert validation["ready"], validation
        tested = await client.post(
            path + "/test",
            json={"expected_plan_hash": validation["plan_hash"], "readiness_hash": validation["readiness_hash"]},
            headers=headers,
        )
        assert tested.status_code == 202, tested.text
        jid = uuid.UUID(tested.json()["jobs_id"])
        async with factory() as worker:
            outcome = await run_schedule_now(worker, sid, tenant_id=tenant_id, existing_job_id=jid)
            assert outcome.reason == "done", outcome
        async with factory() as worker:
            duplicate = await run_schedule_now(worker, sid, tenant_id=tenant_id, existing_job_id=jid)
            assert duplicate.reason == "done"
        assert len(calls) == 2
        async with factory() as db:
            await set_tenant_context(db, str(tenant_id))
            reports = (await db.scalars(select(Report).where(Report.tenant_id == tenant_id))).all()
            assert len(reports) == 1 and reports[0].auto_refresh == "off" and reports[0].source_run_id == jid
            assert "Test ·" in reports[0].rendered_html and "Trial Balance" in reports[0].rendered_html
        detail = (await client.get(path, headers=headers)).json()
        assert detail["plan_status"] == "pending_approval" and detail["plan_version"] == 0
        edited = await client.patch(
            path, json={"cron_expression": "0 9 * * 1", "timezone": "America/Los_Angeles"}, headers=headers
        )
        assert edited.status_code == 200
        validation = (await client.post(path + "/validate", json={}, headers=headers)).json()
        approved = await client.post(
            path + "/approve",
            json={"plan_hash": validation["plan_hash"], "readiness_hash": validation["readiness_hash"]},
            headers=headers,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["next_run_at"] and approved.json()["plan_version"] == 1
        assert (await client.post(path + "/pause", headers=headers)).status_code == 200
        assert (await client.post(path + "/resume", headers=headers)).status_code == 200

        entered, release = asyncio.Event(), asyncio.Event()

        async def wait_compile(db, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await compile_synthetic(db, **kwargs)

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", wait_compile)
        pending = asyncio.create_task(client.patch(path, json={"instruction": "Changed plan"}, headers=headers))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second = await asyncio.wait_for(client.patch(path, json={"timezone": "UTC"}, headers=headers), 5)
            assert second.status_code == 200
        finally:
            release.set()
        first = await asyncio.wait_for(pending, 5)
        assert first.status_code == 409, first.text
        detail = (await client.get(path, headers=headers)).json()
        assert detail["timezone"] == "UTC" and detail["instruction"] == "Trial balance for Jun 2026"
        assert detail["paused_at"]
        stale_resume = await client.post(path + "/resume", headers=headers)
        assert stale_resume.status_code == 409
        validation = (await client.post(path + "/validate", json={}, headers=headers)).json()
        refreshed = await client.post(
            path + "/approve",
            json={"plan_hash": validation["plan_hash"], "readiness_hash": validation["readiness_hash"]},
            headers=headers,
        )
        assert refreshed.status_code == 200, refreshed.text
        assert (await client.post(path + "/resume", headers=headers)).status_code == 200
        # Another transaction revokes the owner's roles. Reusing its old token cannot authorize a test.
        await i["operator"].execute("DELETE FROM user_roles WHERE user_id=$1", user.id)
        async with factory() as worker:
            blocked = await run_schedule_now(worker, sid, tenant_id=tenant_id)
            assert blocked.reason == "blocked"
        denied = await client.post(
            path + "/test",
            json={"expected_plan_hash": validation["plan_hash"], "readiness_hash": validation["readiness_hash"]},
            headers=headers,
        )
        assert denied.status_code == 403
