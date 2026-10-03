"""Real connections and SIGKILL around synthetic scheduled effects; no live providers."""

import asyncio
import hashlib
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
from app.models.tenant import Tenant, TenantConfig
from app.models.user import User, UserRole
from app.services.jobs.registry import STEP_REGISTRY
from app.workers.tasks.scheduled_jobs import run_due_jobs, run_schedule_now
from tests.conftest import create_test_tenant
from tests.jobs.test_executor import _fake_spec, _seed_job_schedule


@pytest.mark.parametrize(
    "phase",
    [
        "remote_accepted",
        "receipt_committed",
        "concurrent",
        "query_pending",
        "query_complete",
        "query_provider",
        "query_settled",
        "query_effect",
    ],
)
async def test_crash_and_duplicate_delivery_use_durable_job_boundary(tmp_path, monkeypatch, phase):
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1"} and parsed.database == "ecom_netsuite_test"
    engine = create_async_engine(url)
    tid = None
    sink = tmp_path / "synthetic-remote.txt"
    process = None
    actor_id = None
    source_id = None
    binding = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            tenant = await create_test_tenant(db)
            tid = tenant.id
            if phase == "query_provider":
                from app.core.encryption import encrypt_credentials
                from tests.conftest import create_test_user

                actor, _ = await create_test_user(db, tenant)
                actor_id = actor.id
                source_row = McpConnector(
                    tenant_id=tid,
                    provider="bigquery",
                    label="Synthetic recovery",
                    server_url="https://bigquery.googleapis.com",
                    auth_type="service_account",
                    status="active",
                    is_enabled=True,
                    encrypted_credentials=encrypt_credentials(
                        {"service_account_json": {}, "project_id": "synthetic-project"}
                    ),
                )
                db.add(source_row)
                await db.flush()
                source_id = str(source_row.id)
                binding = hashlib.sha256(source_row.encrypted_credentials.encode()).hexdigest()
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
import asyncio, os, signal, uuid, json
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.core.config import settings
from app.services.jobs.registry import STEP_REGISTRY, StepSpec
from app.workers.tasks import scheduled_jobs as worker
async def effect(ctx, params):
    if os.environ['CRASH_PHASE'].startswith('query_'):
        from app.services.jobs.report_queries import report_queries
        with report_queries(ctx) as usage:
            if os.environ['CRASH_PHASE'] == 'query_provider':
                await usage.begin(source_id=os.environ['QUERY_SOURCE'],project_id='synthetic-project',location='US',
                                  query_sha256='a'*64,credential_binding=os.environ['QUERY_BINDING'])
                Path(os.environ['SYNTHETIC_SINK']+'.json').write_text(json.dumps({'job_id':usage.provider_job_id}))
            else:
                await usage.begin()
            with Path(os.environ['SYNTHETIC_SINK']).open('a') as f:
                f.write('accepted\\n'); f.flush(); os.fsync(f.fileno())
            if os.environ['CRASH_PHASE'] in {'query_pending','query_provider'}: os.kill(os.getpid(), signal.SIGKILL)
            await usage.complete({'bytes_processed':1024, 'bytes_billed':1024, 'cache_hit':False, 'job_id':usage.provider_job_id})
            if os.environ['CRASH_PHASE'] == 'query_settled': os.kill(os.getpid(), signal.SIGKILL)
            if os.environ['CRASH_PHASE'] == 'query_effect':
                from app.services import audit_service
                await audit_service.log_event(ctx.db,tenant_id=ctx.tenant_id,category='jobs',
                    action='drive.upload.started',resource_type='schedule_step',resource_id='deliver',job_id=ctx.run_id)
                await ctx.db.commit()
                os.kill(os.getpid(), signal.SIGKILL)
        return {'bytes_processed':1024, 'report_query_bytes':1024}
    with Path(os.environ['SYNTHETIC_SINK']).open('a') as f:
        f.write('accepted\\n'); f.flush(); os.fsync(f.fileno())
    if os.environ['CRASH_PHASE'] == 'remote_accepted': os.kill(os.getpid(), signal.SIGKILL)
    if os.environ['CRASH_PHASE'] == 'concurrent': await asyncio.sleep(0.5)
    return {'ok': True}
async def finalize(*args, **kwargs): os.kill(os.getpid(), signal.SIGKILL)
STEP_REGISTRY['fake.step'] = StepSpec('fake.step','synthetic','write',{},effect,lambda c,p: str(c.run_id))
if os.environ['CRASH_PHASE'].startswith('query_'):
    STEP_REGISTRY['fake.step'] = StepSpec('fake.step','synthetic query','read',{},effect)
if os.environ['CRASH_PHASE'] in {'receipt_committed','query_complete'}: worker._finalize_run = finalize
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
            "QUERY_SOURCE": source_id or "",
            "QUERY_BINDING": binding or "",
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
            assert saved.result_summary["reason"] == (
                "blocked"
                if phase in {"remote_accepted", "query_pending", "query_provider", "query_effect"}
                else "error"
                if phase == "query_settled"
                else "done"
            )
            if phase in {"remote_accepted", "query_pending", "query_provider", "query_effect"}:
                assert saved.result_summary["verification"] == "uncertain"
            if phase.startswith("query_"):
                query = saved.result_summary["report_queries"][0]
                assert query["state"] == ("pending" if phase in {"query_pending", "query_provider"} else "complete")
                if phase in {"query_complete", "query_settled", "query_effect"}:
                    assert saved.result_summary["usage"]["bytes_scanned"] == 1024
                    assert saved.result_summary["usage"]["usd"] is None
            if phase == "query_provider":
                from app.services.jobs.recovery import reconcile_run

                provider = json.loads(sink.with_name(sink.name + ".json").read_text())
                assert query["provider_job_id"] == provider["job_id"]
                reads = []

                async def read(credentials, project_id, job_id, *, location):
                    reads.append(job_id)
                    assert job_id == provider["job_id"]
                    return {**query, "job_id": job_id, "state": "DONE", "bytes_processed": 1024, "bytes_billed": 1024}

                monkeypatch.setattr("app.services.bigquery_service.read_query_receipt", read)
                result = await reconcile_run(recovery, tenant_id=tid, schedule_id=sid, job_id=jid, actor_id=actor_id)
                assert result["verification"] == "reconciled"
                assert saved.result_summary["usage"]["bytes_scanned"] == 1024
                assert saved.status == "failed"
                assert reads == [provider["job_id"]]
            await run_schedule_now(recovery, sid, tenant_id=tid, actor_id=None, existing_job_id=jid)
        assert sink.read_text() == "accepted\n"
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
