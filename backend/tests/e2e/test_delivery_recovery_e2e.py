"""Real SIGKILL/transaction boundaries, with a disk-backed synthetic Drive."""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.feature_flag import TenantFeatureFlag
from app.models.job import Job
from app.models.mcp_connector import McpConnector
from app.models.pipeline import Schedule
from app.models.report import Report
from app.models.tenant import Tenant, TenantConfig
from app.models.user import User, UserRole
from app.services.jobs.recovery import reconcile_run
from app.services.report import report_delivery as delivery
from app.workers.tasks.scheduled_jobs import run_due_jobs, run_schedule_now
from tests.conftest import create_test_tenant, create_test_user
from tests.jobs.test_delivery_reconciliation import EvidenceDrive
from tests.jobs.test_executor import _seed_job_schedule
from tests.report.test_report_delivery import _add_sheets_connector, _seed_report
from tests.test_dedicated_runtime import installation  # noqa: F401 — real restricted-role fixture


class DiskDrive(EvidenceDrive):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.lose_receipt = False
        if path.exists():
            data = json.loads(path.read_text())
            for attr in ("_folders", "_files"):
                setattr(self, attr, {(tuple(tuple(p) for p in k[0]), k[1]): v for k, v in data[attr]})
            self._next_id = data["next_id"]
            self.calls = data["calls"]

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "_folders": list(self._folders.items()),
                    "_files": list(self._files.items()),
                    "next_id": self._next_id,
                    "calls": self.calls,
                }
            )
        )

    async def create_folder(self, **kwargs):
        result = await super().create_folder(**kwargs)
        self.save()
        return result

    async def upload_new(self, **kwargs):
        result = await super().upload_new(**kwargs)
        self.save()
        if kwargs["app_properties"]["kind"] == "xlsx":
            os.kill(os.getpid(), signal.SIGKILL)
        return result


@pytest.mark.parametrize("phase", ["accepted", "reconcile_commit", "concurrent", "revoked_api"])
async def test_content_recovery_survives_process_death_and_duplicate_operators(tmp_path, monkeypatch, phase):
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.database == "ecom_netsuite_test"
    engine = create_async_engine(url)
    tid = None
    process = None
    sink = tmp_path / "synthetic-drive.json"
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            tenant = await create_test_tenant(db)
            user, _ = await create_test_user(db, tenant)
            tid, uid = tenant.id, user.id
            report = await _seed_report(db, tenant, user)
            await _add_sheets_connector(db, tid)
            schedule = await _seed_job_schedule(
                db,
                tenant,
                next_run_at=None,
                plan_json={
                    "steps": [
                        {"id": "compose", "type": "report.compose", "params": {}},
                        {"id": "upload", "type": "drive.upload", "params": {"report_step": "compose"}},
                    ]
                },
            )
            sid, rid = schedule.id, report.id
            job = Job(
                tenant_id=tid,
                job_type="scheduled_job",
                status="pending",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
                parameters={
                    "schedule_id": str(sid),
                    "dispatch_ready": True,
                    "recovery_version": 1,
                },
            )
            db.add(job)
            await db.commit()
            jid = job.id
        source = """
import asyncio, os, signal, uuid
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.core.config import settings
from app.models.report import Report
from app.services import audit_service
from app.services.jobs.registry import STEP_REGISTRY
from app.services.jobs.recovery import reconcile_run
from app.services.report import report_delivery as delivery
from app.workers.tasks.scheduled_jobs import run_schedule_now
from tests.jobs.test_executor import _fake_spec
from tests.e2e.test_delivery_recovery_e2e import DiskDrive
settings.ENCRYPTION_KEY = os.environ['TEST_ENCRYPTION_KEY']
drive = DiskDrive(Path(os.environ['SINK']))
delivery._build_drive_client = lambda *args: drive
delivery._render_pdf_bytes = lambda report: b'synthetic-pdf'
delivery._render_xlsx_bytes = lambda report: b'synthetic-xlsx'
async def compose(ctx, params):
    report = await ctx.db.get(Report, uuid.UUID(os.environ['REPORT_ID']))
    return {'report': report, 'report_id': str(report.id)}
STEP_REGISTRY['report.compose'] = _fake_spec('read', compose)
original_audit = audit_service.log_event
async def crash_audit(*args, **kwargs):
    result = await original_audit(*args, **kwargs)
    if kwargs.get('action') == 'jobs.run.reconciled': os.kill(os.getpid(), signal.SIGKILL)
    return result
async def main():
    e = create_async_engine(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    async with AsyncSession(e, expire_on_commit=False) as db:
        params = dict(tenant_id=uuid.UUID(os.environ['TENANT_ID']), actor_id=uuid.UUID(os.environ['USER_ID']))
        sid, jid = uuid.UUID(os.environ['SCHEDULE_ID']), uuid.UUID(os.environ['JOB_ID'])
        if os.environ['PHASE'] == 'execute':
            await run_schedule_now(db, sid, existing_job_id=jid, **params)
        else:
            audit_service.log_event = crash_audit
            await reconcile_run(db, schedule_id=sid, job_id=jid, **params)
    await e.dispose()
asyncio.run(main())
"""
        env = {
            **os.environ,
            "SINK": str(sink),
            "TEST_ENCRYPTION_KEY": settings.ENCRYPTION_KEY,
            "TENANT_ID": str(tid),
            "USER_ID": str(uid),
            "SCHEDULE_ID": str(sid),
            "REPORT_ID": str(rid),
            "JOB_ID": str(jid),
            "PHASE": "execute",
        }

        async def crash_child():
            nonlocal process
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", source, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), 20)
            assert process.returncode == -signal.SIGKILL, stderr.decode()

        await crash_child()
        drive = DiskDrive(sink)
        assert drive.calls.count("upload_new") == 2
        monkeypatch.setattr(delivery, "_build_drive_client", lambda *args: drive)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await run_due_jobs(db, tid)
            assert (await db.get(Job, jid)).result_summary["verification"] == "uncertain"
        if phase == "revoked_api":
            from httpx import ASGITransport, AsyncClient

            from app.core.database import get_db
            from app.main import create_app
            from tests.conftest import make_auth_headers

            lookup = drive.find_unique
            async with AsyncSession(engine) as db:
                role_id = await db.scalar(select(UserRole.role_id).where(UserRole.user_id == uid))

            async def revoke_role(**kwargs):
                found = await lookup(**kwargs)
                if (kwargs.get("app_properties") or {}).get("kind") == "xlsx":
                    async with AsyncSession(engine) as revoked:
                        await revoked.execute(UserRole.__table__.delete().where(UserRole.user_id == uid))
                        await revoked.commit()
                return found

            monkeypatch.setattr(drive, "find_unique", revoke_role)
            application = create_app()

            async def request_db():
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    yield session

            application.dependency_overrides[get_db] = request_db
            async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
                response = await client.post(
                    f"/api/v1/schedules/{sid}/runs/{jid}/reconcile", headers=make_auth_headers(user)
                )
                assert response.status_code == 403, response.text
            async with AsyncSession(engine) as db:
                assert (await db.get(Job, jid)).result_summary["verification"] == "uncertain"
                assert await db.scalar(
                    select(AuditEvent.id).where(
                        AuditEvent.job_id == jid, AuditEvent.action == "jobs.run.reconciliation_denied"
                    )
                )
                db.add(UserRole(tenant_id=tid, user_id=uid, role_id=role_id))
                await db.commit()
            monkeypatch.setattr(drive, "find_unique", lookup)
        if phase == "reconcile_commit":
            env["PHASE"] = "reconcile"
            await crash_child()
            async with AsyncSession(engine) as db:
                assert (await db.get(Job, jid)).result_summary["verification"] == "uncertain"
                assert not await db.scalar(
                    select(AuditEvent.id).where(AuditEvent.job_id == jid, AuditEvent.action == "jobs.run.reconciled")
                )
        if phase == "concurrent":
            entered, release = asyncio.Event(), asyncio.Event()
            lookup = drive.find_unique

            async def wait_read(**kwargs):
                entered.set()
                await release.wait()
                return await lookup(**kwargs)

            monkeypatch.setattr(drive, "find_unique", wait_read)
            async with AsyncSession(engine, expire_on_commit=False) as first, AsyncSession(engine) as second:
                pending = asyncio.create_task(
                    reconcile_run(first, tenant_id=tid, schedule_id=sid, job_id=jid, actor_id=uid)
                )
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    duplicate = await reconcile_run(second, tenant_id=tid, schedule_id=sid, job_id=jid, actor_id=uid)
                    assert duplicate["verification"] == "uncertain"
                    run = await run_schedule_now(second, sid, tenant_id=tid, actor_id=uid, existing_job_id=jid)
                    assert run.reason == "blocked"
                finally:
                    release.set()
                    result = await pending
                assert result["verification"] == "verified"
        async with AsyncSession(engine, expire_on_commit=False) as db:
            result = await reconcile_run(db, tenant_id=tid, schedule_id=sid, job_id=jid, actor_id=uid)
            assert result["verification"] == "verified"
            assert (await db.get(Job, jid)).status == "completed"
            assert (
                len(
                    (
                        await db.scalars(
                            select(AuditEvent).where(
                                AuditEvent.job_id == jid, AuditEvent.action == "jobs.run.reconciled"
                            )
                        )
                    ).all()
                )
                == 1
            )
            again = await run_schedule_now(db, sid, tenant_id=tid, actor_id=uid, existing_job_id=jid)
            assert again.reason == "done"
            assert (await db.get(Schedule, sid)).paused_at is not None
        assert drive.calls.count("upload_new") == 2
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        if tid:
            async with AsyncSession(engine) as db:
                for connector in (await db.scalars(select(McpConnector).where(McpConnector.tenant_id == tid))).all():
                    await db.delete(connector)
                await db.flush()
                for model in [
                    Schedule,
                    Job,
                    AuditEvent,
                    Report,
                    UserRole,
                    User,
                    TenantFeatureFlag,
                    TenantConfig,
                    Tenant,
                ]:
                    await db.execute(
                        model.__table__.delete().where(model.id == tid)
                        if model is Tenant
                        else model.__table__.delete().where(model.tenant_id == tid)
                    )
                await db.commit()
        await engine.dispose()


async def test_reconciliation_with_actual_restricted_runtime_role(installation, monkeypatch):  # noqa: F811
    from app.core.database import set_tenant_context
    from tests.jobs import test_delivery_reconciliation as fixtures

    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    async with AsyncSession(installation["engine"], expire_on_commit=False) as db:
        await set_tenant_context(db, str(installation["company"]))
        tenant = await db.get(Tenant, installation["company"])

        async def existing_company(*args, **kwargs):
            return tenant

        monkeypatch.setattr(fixtures, "create_test_tenant", existing_company)
        tenant, user, schedule, job, connector, drive = await fixtures.uncertain_delivery(db, monkeypatch)
        result = await fixtures.reconcile(db, tenant, user, schedule, job)
        assert result["verification"] == "verified"
        assert job.status == "completed"
        assert drive.calls.count("upload_new") == 2
