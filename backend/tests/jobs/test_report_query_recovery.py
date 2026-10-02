"""Report queries retain identity and usage without replaying provider work."""

import hashlib
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.job import Job
from app.services.jobs.registry import StepContext
from app.services.jobs.report_queries import ReportQueries, ReportQueryUnknownError


async def pending_query(db, admin_user):
    user, _ = admin_user
    job = Job(tenant_id=user.tenant_id, job_type="scheduled_job", status="running", parameters={})
    db.add(job)
    await db.flush()
    ctx = StepContext(job_id=uuid4(), run_id=job.id, tenant_id=user.tenant_id, db=db, budget={"bytes_scanned": 2000})
    scope = ReportQueries(ctx)
    await scope.begin(
        source_id=str(uuid4()),
        query_sha256=hashlib.sha256(b"SELECT 1").hexdigest(),
        project_id="synthetic-project",
        location="US",
        credential_binding="a" * 64,
    )
    return scope, job


async def test_identity_committed_before_dispatch(db, admin_user):
    scope, job = await pending_query(db, admin_user)
    await db.refresh(job)
    receipt = job.result_summary["report_queries"][0]
    assert receipt["provider_job_id"] == scope.provider_job_id
    assert receipt["provider_job_id"].startswith("ss_report_")
    assert receipt["project_id"] == "synthetic-project" and receipt["location"] == "US"
    assert receipt["state"] == "pending"


async def test_cancellation_settles_usage_but_cannot_continue(db, admin_user):
    scope, job = await pending_query(db, admin_user)
    job.status = "cancelled"
    await db.commit()
    with pytest.raises(ReportQueryUnknownError):
        await scope.complete({"bytes_processed": 100, "bytes_billed": 100, "job_id": scope.provider_job_id})
    await db.refresh(job)
    assert job.status == "cancelled"
    assert job.result_summary["report_queries"][0]["bytes_processed"] == 100
    with pytest.raises(ReportQueryUnknownError):
        await scope.begin()


async def test_wrong_provider_identity_never_settles(db, admin_user):
    scope, job = await pending_query(db, admin_user)
    with pytest.raises(ReportQueryUnknownError):
        await scope.complete({"bytes_processed": 0, "job_id": "wrong"})
    await db.refresh(job)
    assert job.result_summary["report_queries"][0]["state"] == "pending"


async def test_definite_no_dispatch_can_settle_zero(db, admin_user):
    scope, job = await pending_query(db, admin_user)
    await scope.not_dispatched()
    await db.refresh(job)
    assert job.result_summary["report_queries"][0]["state"] == "not_dispatched"
    from app.services.jobs.report_queries import queries_uncertain, query_bytes

    assert not queries_uncertain(job.result_summary) and query_bytes(job.result_summary) == 0


async def test_sdk_pins_job_id_and_disables_query_retry(monkeypatch):
    from app.services.bigquery_service import execute_query

    client = MagicMock()
    job = client.query.return_value
    job.result.return_value.schema = []
    monkeypatch.setattr("app.services.bigquery_service._get_client", lambda *a, **kw: client)
    await execute_query({}, "synthetic-project", "SELECT 1", job_id="ss_report_test")
    assert client.query.call_args.kwargs["job_id"] == "ss_report_test"
    assert client.query.call_args.kwargs["job_retry"] is None


async def test_provider_readback_never_submits(monkeypatch):
    from app.services.bigquery_service import read_query_receipt

    client = MagicMock()
    job = client.get_job.return_value
    job.state = "DONE"
    job.job_id = "ss_report_test"
    job.project = "synthetic-project"
    job.location = "US"
    job.query = "SELECT 1"
    job.total_bytes_processed = 42
    job.total_bytes_billed = 100
    job.cache_hit = False
    job.error_result = None
    job.maximum_bytes_billed = 2000
    monkeypatch.setattr("app.services.bigquery_service._get_client", lambda *a, **kw: client)
    result = await read_query_receipt({}, "synthetic-project", "ss_report_test", location="US")
    assert result["bytes_processed"] == 42
    assert result["query_sha256"] == hashlib.sha256(b"SELECT 1").hexdigest()
    client.query.assert_not_called()
    job.result.assert_not_called()


@pytest.mark.parametrize(
    "mode",
    [
        "complete",
        "provider_error",
        "not_found",
        "running",
        "wrong_hash",
        "wrong_project",
        "wrong_location",
        "wrong_cap",
        "missing_usage",
        "source_changed",
        "cancelled",
    ],
)
async def test_reconciliation_accounts_without_replay(client, db, admin_user, monkeypatch, mode):
    from app.services.jobs.recovery import reconcile_run
    from app.workers.tasks import scheduled_jobs as jobs
    from tests.e2e.test_scheduled_report_delivery_e2e import setup_report_workflow

    user, headers = admin_user
    row, source, drive, calls = await setup_report_workflow(
        db, client, user, headers, monkeypatch, with_delivery=False, failure="late_error"
    )
    tid, sid, uid = user.tenant_id, row.id, user.id
    await jobs.run_due_jobs(db, tid)
    job = (await db.scalars(select(Job).where(Job.tenant_id == tid))).one()
    intent = job.result_summary["report_queries"][-1]
    before = list(calls)
    reads = []

    async def read(credentials, project_id, job_id, *, location):
        reads.append(job_id)
        assert (
            project_id == intent["project_id"]
            and job_id == intent["provider_job_id"]
            and location == intent["location"]
        )
        if mode == "not_found":
            raise LookupError("404")
        result = {
            **intent,
            "state": "DONE",
            "job_id": job_id,
            "bytes_processed": 55,
            "bytes_billed": 100,
            "provider_failed": mode == "provider_error",
        }
        if mode == "running":
            result["state"] = "RUNNING"
        if mode == "wrong_hash":
            result["query_sha256"] = "0" * 64
        if mode == "wrong_project":
            result["project_id"] = "elsewhere"
        if mode == "wrong_location":
            result["location"] = "EU"
        if mode == "wrong_cap":
            result["maximum_bytes_billed"] = 1
        if mode == "missing_usage":
            result["bytes_processed"] = None
        return result

    monkeypatch.setattr("app.services.bigquery_service.read_query_receipt", read)
    if mode == "source_changed":
        source.status = "revoked"
        await db.commit()
    if mode == "cancelled":
        job.status = "cancelled"
        await db.commit()
    result = await reconcile_run(db, tenant_id=tid, schedule_id=sid, job_id=job.id, actor_id=uid)
    await db.refresh(job)
    await db.refresh(row)
    if mode in {"complete", "provider_error", "cancelled"}:
        assert result["verification"] == "reconciled"
        assert job.result_summary["usage"]["bytes_scanned"] == 1079
        assert job.result_summary["usage"]["usd"] is None
        assert job.result_summary["execution_complete"] is False
        assert job.status == ("cancelled" if mode == "cancelled" else "failed")
        assert row.paused_at is not None
        again = await reconcile_run(db, tenant_id=tid, schedule_id=sid, job_id=job.id, actor_id=uid)
        assert again == result and len(reads) == 1
    else:
        assert result["verification"] == "uncertain"
        assert job.result_summary["report_queries"][-1]["state"] == "pending"
    assert calls == before and drive.calls == []


async def test_saved_report_usd_ceiling_rejected_during_inspection():
    from app.models.pipeline import Schedule
    from app.services.jobs.inspection import inspect_plan

    row = Schedule(
        id=uuid4(),
        tenant_id=uuid4(),
        plan_version=1,
        plan_json={"steps": [{"id": "r", "type": "report.compose", "params": {"report_id": str(uuid4())}}]},
        budget_json={"usd": 1},
    )
    assert any("pricing contract" in b for b in inspect_plan(row)["blockers"])


@pytest.mark.parametrize("failure", ["client", "validation"])
async def test_known_pre_dispatch_failure_records_zero_without_submit(db, admin_user, monkeypatch, failure):
    from app.services.bigquery_service import BigQueryClientError, QueryNotDispatchedError, execute_query

    scope, job = await pending_query(db, admin_user)
    client = MagicMock()
    if failure == "client":

        def broken(*a, **kw):
            raise BigQueryClientError("synthetic invalid credentials")

        monkeypatch.setattr("app.services.bigquery_service._get_client", broken)
    else:
        monkeypatch.setattr("app.services.bigquery_service._get_client", lambda *a, **kw: client)
    with pytest.raises(QueryNotDispatchedError):
        await execute_query(
            {},
            "synthetic-project",
            "DELETE FROM t" if failure == "validation" else "SELECT 1",
            job_id=scope.provider_job_id,
        )
    await scope.not_dispatched()
    await db.refresh(job)
    assert job.result_summary["report_queries"][0]["state"] == "not_dispatched"
    client.query.assert_not_called()


@pytest.mark.parametrize("change", ["permission", "connector", "concurrent", "tenant"])
async def test_recovery_authorization_and_concurrency(client, db, admin_user, monkeypatch, change):
    from sqlalchemy import delete

    from app.models.user import UserRole
    from app.services.jobs.recovery import reconcile_run
    from app.workers.tasks import scheduled_jobs as jobs
    from tests.e2e.test_scheduled_report_delivery_e2e import setup_report_workflow

    user, headers = admin_user
    row, source, drive, calls = await setup_report_workflow(
        db, client, user, headers, monkeypatch, with_delivery=False, failure="late_error"
    )
    tid, sid, uid = user.tenant_id, row.id, user.id
    await jobs.run_due_jobs(db, tid)
    job = (await db.scalars(select(Job).where(Job.tenant_id == tid))).one()
    jid = job.id
    intent = job.result_summary["report_queries"][-1]
    reads = []

    async def read(*args, **kwargs):
        reads.append(1)
        if change == "permission":
            await db.execute(delete(UserRole).where(UserRole.user_id == uid))
        elif change == "connector":
            source.status = "revoked"
        elif change == "concurrent":
            duplicate = await reconcile_run(db, tenant_id=tid, schedule_id=sid, job_id=jid, actor_id=uid)
            assert duplicate["verification"] == "uncertain" and "in progress" in duplicate["detail"]
        await db.flush()
        return {
            **intent,
            "job_id": intent["provider_job_id"],
            "state": "DONE",
            "bytes_processed": 55,
            "bytes_billed": 100,
        }

    monkeypatch.setattr("app.services.bigquery_service.read_query_receipt", read)
    if change in {"permission", "tenant"}:
        with pytest.raises(PermissionError):
            await reconcile_run(
                db, tenant_id=uuid4() if change == "tenant" else tid, schedule_id=sid, job_id=jid, actor_id=uid
            )
    else:
        result = await reconcile_run(db, tenant_id=tid, schedule_id=sid, job_id=jid, actor_id=uid)
        assert result["verification"] == ("reconciled" if change == "concurrent" else "uncertain")
    await db.refresh(job)
    if change != "concurrent":
        assert job.result_summary["report_queries"][-1]["state"] == "pending"
    assert len(reads) == (0 if change == "tenant" else 1)
    assert len(calls) == 2 and drive.calls == []


async def test_missing_billed_usage_remains_pending(db, admin_user):
    scope, job = await pending_query(db, admin_user)
    with pytest.raises(ReportQueryUnknownError):
        await scope.complete({"bytes_processed": 42, "job_id": scope.provider_job_id})
    await db.refresh(job)
    assert job.result_summary["report_queries"][0]["state"] == "pending"


async def test_cancelled_query_stops_full_report_without_scheduling_retry(client, db, admin_user, monkeypatch):
    from app.mcp.tools import bigquery_tools
    from app.workers.tasks import scheduled_jobs as jobs
    from tests.e2e.test_scheduled_report_delivery_e2e import setup_report_workflow

    user, headers = admin_user
    row, source, drive, calls = await setup_report_workflow(db, client, user, headers, monkeypatch, with_delivery=False)
    tid = user.tenant_id
    query = bigquery_tools.execute_query

    async def cancel(**kwargs):
        result = await query(**kwargs)
        job = (await db.scalars(select(Job).where(Job.tenant_id == tid))).one()
        job.status = "cancelled"
        await db.commit()
        return result

    monkeypatch.setattr(bigquery_tools, "execute_query", cancel)
    await jobs.run_due_jobs(db, tid)
    await db.refresh(row)
    job = (await db.scalars(select(Job).where(Job.tenant_id == tid))).one()
    assert job.status == "cancelled"
    assert job.result_summary["reason"] == "blocked"
    assert row.retry_job_id is None
    assert job.result_summary["usage"]["bytes_scanned"] == 1024
    assert len(calls) == 1 and drive.calls == []


async def test_cancellation_between_queries_remains_a_stop_after_begin_error(db, admin_user):
    scope, job = await pending_query(db, admin_user)
    await scope.complete({"bytes_processed": 100, "bytes_billed": 100, "job_id": scope.provider_job_id})
    job.status = "cancelled"
    await db.commit()
    with pytest.raises(ReportQueryUnknownError):
        await scope.begin()
    with pytest.raises(ReportQueryUnknownError, match="Report stopped"):
        scope.check()
