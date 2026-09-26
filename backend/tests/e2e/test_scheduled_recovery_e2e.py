"""Real connections and SIGKILL around synthetic scheduled effects; no live providers."""

import asyncio
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.feature_flag import TenantFeatureFlag
from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.tenant import Tenant, TenantConfig
from app.services.jobs.registry import STEP_REGISTRY
from app.workers.tasks.scheduled_jobs import run_due_jobs, run_schedule_now
from tests.conftest import create_test_tenant
from tests.jobs.test_executor import _fake_spec, _seed_job_schedule


@pytest.mark.parametrize("phase", ["remote_accepted", "receipt_committed", "concurrent"])
async def test_crash_and_duplicate_delivery_use_durable_job_boundary(tmp_path, monkeypatch, phase):
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1"} and parsed.database == "ecom_netsuite_test"
    engine = create_async_engine(url)
    tid = None
    sink = tmp_path / "synthetic-remote.txt"
    process = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            tenant = await create_test_tenant(db)
            tid = tenant.id
            schedule = await _seed_job_schedule(
                db, tenant, plan_json={"steps": [{"id": "effect", "type": "fake.step", "params": {}}]}, next_run_at=None
            )
            sid = schedule.id
            job = Job(
                tenant_id=tid,
                job_type="scheduled_job",
                status="pending",
                parameters={"schedule_id": str(sid), "recovery_version": 1, "dispatch_ready": True},
                created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            db.add(job)
            await db.commit()
            jid = job.id
        source = """
import asyncio, os, signal, uuid
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.core.config import settings
from app.services.jobs.registry import STEP_REGISTRY, StepSpec
from app.workers.tasks import scheduled_jobs as worker
async def effect(ctx, params):
    with Path(os.environ['SYNTHETIC_SINK']).open('a') as f:
        f.write('accepted\\n'); f.flush(); os.fsync(f.fileno())
    if os.environ['CRASH_PHASE'] == 'remote_accepted': os.kill(os.getpid(), signal.SIGKILL)
    if os.environ['CRASH_PHASE'] == 'concurrent': await asyncio.sleep(0.5)
    return {'ok': True}
async def finalize(*args, **kwargs): os.kill(os.getpid(), signal.SIGKILL)
STEP_REGISTRY['fake.step'] = StepSpec('fake.step','synthetic','write',{},effect,lambda c,p: str(c.run_id))
if os.environ['CRASH_PHASE'] == 'receipt_committed': worker._finalize_run = finalize
async def main():
    e = create_async_engine(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    async with AsyncSession(e, expire_on_commit=False) as db:
        await worker.run_schedule_now(db,uuid.UUID(os.environ['SCHEDULE_ID']),tenant_id=uuid.UUID(os.environ['TENANT_ID']),actor_id=None,existing_job_id=uuid.UUID(os.environ['JOB_ID']))
    await e.dispose()
asyncio.run(main())
"""
        env = {
            **os.environ,
            "SYNTHETIC_SINK": str(sink),
            "CRASH_PHASE": phase,
            "TENANT_ID": str(tid),
            "SCHEDULE_ID": str(sid),
            "JOB_ID": str(jid),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", source, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        async def forbidden(ctx, params):
            raise AssertionError("Recovery repeated an external effect")

        monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("write", forbidden, lambda c, p: str(c.run_id)))
        if phase == "concurrent":
            for _ in range(200):
                if sink.exists():
                    break
                await asyncio.sleep(0.02)
            assert sink.exists()
            async with AsyncSession(engine, expire_on_commit=False) as second:
                result = await run_schedule_now(second, sid, tenant_id=tid, actor_id=None, existing_job_id=jid)
                assert result.reason == "blocked"
        _, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == (0 if phase == "concurrent" else -signal.SIGKILL), stderr.decode()
        async with AsyncSession(engine, expire_on_commit=False) as recovery:
            await run_due_jobs(recovery, tid)
            saved = await recovery.get(Job, jid)
            assert saved.result_summary["reason"] == ("blocked" if phase == "remote_accepted" else "done")
            if phase == "remote_accepted":
                assert saved.result_summary["verification"] == "uncertain"
            await run_schedule_now(recovery, sid, tenant_id=tid, actor_id=None, existing_job_id=jid)
        assert sink.read_text() == "accepted\n"
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        if tid:
            async with AsyncSession(engine) as db:
                for model in [Schedule, Job, AuditEvent, TenantFeatureFlag, TenantConfig, Tenant]:
                    await db.execute(
                        model.__table__.delete().where(model.id == tid)
                        if model is Tenant
                        else model.__table__.delete().where(model.tenant_id == tid)
                    )
                await db.commit()
        await engine.dispose()
