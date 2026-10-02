"""Content-bound recovery against synthetic Drive, with real tenant/job rows."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.job import Job
from app.services.jobs.registry import STEP_REGISTRY
from app.services.report import report_delivery as delivery
from app.workers.tasks import scheduled_jobs as jobs
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers
from tests.jobs.test_executor import _fake_spec, _seed_job_schedule
from tests.report.test_report_delivery import FakeDriveClient, _add_sheets_connector, _seed_report


class EvidenceDrive(FakeDriveClient):
    def __init__(self):
        super().__init__([])
        self.lose_receipt = True
        self.evidence_mode = "exact"

    async def upload_new(self, **kwargs):
        result = await super().upload_new(**kwargs)
        result.update(
            sha256Checksum=hashlib.sha256(kwargs["content"]).hexdigest(),
            size=str(len(kwargs["content"])),
            mimeType=kwargs["mime_type"],
            appProperties=kwargs["app_properties"],
            parents=[kwargs["parent_id"]],
            id=result["file_id"],
        )
        if self.lose_receipt and kwargs["app_properties"]["kind"] == "xlsx":
            raise TimeoutError("remote accepted; response lost")
        return result

    async def update_existing(self, *, file_id, content, mime_type, app_properties=None):
        result = await super().update_existing(file_id=file_id, content=content, mime_type=mime_type)
        result.update(
            sha256Checksum=hashlib.sha256(content).hexdigest(),
            size=str(len(content)),
            mimeType=mime_type,
            appProperties=app_properties,
            id=file_id,
        )
        return result

    async def find_unique(self, *, parent_id, mime_type, app_properties=None, name=None):
        self.calls.append("read_evidence")
        if self.evidence_mode == "unavailable":
            raise TimeoutError("read-back unavailable")
        if self.evidence_mode == "duplicate":
            raise ValueError("ambiguous provider identity")
        if mime_type == "application/vnd.google-apps.folder":
            found = self._folders.get(self._key(name, parent_id, app_properties))
            return {"id": found} if found else None
        record = self._files.get(self._key(name, parent_id, app_properties))
        if not record or (self.evidence_mode == "partial" and app_properties["kind"] == "xlsx"):
            return None
        result = dict(record)
        if self.evidence_mode == "old_attempt":
            result["appProperties"] = {k: v for k, v in result["appProperties"].items() if k != "delivery_attempt"}
        if self.evidence_mode == "stale":
            result["sha256Checksum"] = "0" * 64
        if self.evidence_mode == "missing_hash":
            result.pop("sha256Checksum")
        if self.evidence_mode == "wrong_scope":
            result["appProperties"] = {"schedule_id": str(uuid.uuid4())}
        return result


async def uncertain_delivery(db, monkeypatch, *, with_query=False):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant, role_name="admin")
    report = await _seed_report(db, tenant, user)
    connector = await _add_sheets_connector(db, tenant.id)
    drive = EvidenceDrive()
    monkeypatch.setattr(delivery, "_build_drive_client", lambda *a: drive)
    monkeypatch.setattr(delivery, "_render_pdf_bytes", lambda r: b"synthetic-pdf")
    monkeypatch.setattr(delivery, "_render_xlsx_bytes", lambda r: b"synthetic-xlsx")

    async def compose(ctx, params):
        if with_query:
            from app.services.jobs.report_queries import report_queries

            with report_queries(ctx) as usage:
                await usage.begin()
                await usage.complete({"bytes_processed": 1024, "bytes_billed": 1024, "job_id": usage.provider_job_id})
        return {
            "report_id": str(report.id),
            "report": report,
            "bytes_processed": 1024 if with_query else 0,
            "report_query_bytes": 1024 if with_query else 0,
        }

    monkeypatch.setitem(STEP_REGISTRY, "report.compose", _fake_spec("read", compose))
    plan = {
        "steps": [
            {"id": "compose", "type": "report.compose", "params": {}},
            {"id": "upload", "type": "drive.upload", "params": {"report_step": "compose"}},
        ]
    }
    schedule = await _seed_job_schedule(db, tenant, plan_json=plan, next_run_at=None)
    await db.commit()
    result = await jobs.run_schedule_now(db, schedule.id, tenant_id=tenant.id, actor_id=user.id)
    assert result.reason == "blocked"
    for row in (tenant, user, schedule, connector, report):
        await db.refresh(row)
    return tenant, user, schedule, await db.get(Job, result.jobs_row_id), connector, drive


async def reconcile(db, tenant, user, schedule, job):
    from app.services.jobs.recovery import reconcile_run

    result = await reconcile_run(db, tenant_id=tenant.id, schedule_id=schedule.id, job_id=job.id, actor_id=user.id)
    for row in (tenant, user, schedule, job):
        await db.refresh(row)
    return result


async def test_content_bound_intent_exists_before_outbound_call(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    events = (await db.scalars(select(AuditEvent).where(AuditEvent.job_id == job.id))).all()
    prepared = next(e for e in events if e.action == "report.delivery.prepared")
    assert prepared.payload["step_id"] == "upload"
    assert prepared.payload["connector_id"] == str(connector.id)
    assert prepared.payload["content"]["pdf"]["sha256"] == hashlib.sha256(b"synthetic-pdf").hexdigest()
    assert job.result_summary["step_receipts"]["compose"]["report_id"]


async def test_exact_content_reconciliation_is_read_only_and_terminal(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    before = list(drive.calls)
    result = await reconcile(db, tenant, user, schedule, job)
    assert result["verification"] == "verified"
    assert job.status == "completed"
    assert job.result_summary["reason"] == "done"
    assert schedule.paused_at is not None  # explicit, separate operator resume
    assert set(drive.calls[len(before) :]) == {"read_evidence"}
    again = await reconcile(db, tenant, user, schedule, job)
    assert again == result
    replay = await jobs.run_schedule_now(db, schedule.id, tenant_id=tenant.id, actor_id=user.id, existing_job_id=job.id)
    assert replay.reason == "done"
    assert drive.calls.count("upload_new") == 2
    assert (
        len(
            (
                await db.scalars(
                    select(AuditEvent).where(AuditEvent.job_id == job.id, AuditEvent.action == "jobs.run.reconciled")
                )
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize(
    "mode", ["partial", "stale", "old_attempt", "missing_hash", "wrong_scope", "unavailable", "duplicate"]
)
async def test_missing_or_ambiguous_evidence_stays_fenced(db, monkeypatch, mode):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    drive.evidence_mode = mode
    result = await reconcile(db, tenant, user, schedule, job)
    assert result["verification"] == "uncertain"
    assert job.status == "failed"
    assert schedule.paused_at
    assert drive.calls.count("upload_new") == 2


@pytest.mark.parametrize(
    "change", ["disabled", "credentials", "drive", "missing_receipt", "agent_intent", "unexecuted_step"]
)
async def test_lifecycle_and_unaccounted_work_cannot_be_verified(db, monkeypatch, change):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    if change == "disabled":
        connector.is_enabled = False
    elif change == "credentials":
        connector.encrypted_credentials += "changed"
    elif change == "drive":
        connector.metadata_json = {"shared_drive_id": "different"}
    elif change == "missing_receipt":
        job.result_summary = {**job.result_summary, "step_receipts": {}}
    elif change == "agent_intent":
        from app.services import audit_service

        await audit_service.log_event(
            db,
            tenant_id=tenant.id,
            category="jobs",
            action="agent.review.started",
            resource_type="job",
            resource_id=str(job.id),
            job_id=job.id,
        )
    else:
        job.parameters = {
            **job.parameters,
            "plan": {"steps": [*job.parameters["plan"]["steps"], {"id": "later", "type": "recon.run", "params": {}}]},
        }
    await db.commit()
    result = await reconcile(db, tenant, user, schedule, job)
    assert result["verification"] == "uncertain"
    assert job.status == "failed"


async def test_api_authorization_scope_resume_and_redelivery(client, db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    path = f"/api/v1/schedules/{schedule.id}/runs/{job.id}/reconcile"
    other_tenant = await create_test_tenant(db)
    other_user, _ = await create_test_user(db, other_tenant, role_name="admin")
    viewer, _ = await create_test_user(db, tenant, role_name="viewer")
    await db.commit()
    assert (await client.post(path, headers=make_auth_headers(other_user))).status_code == 404
    assert (await client.post(path, headers=make_auth_headers(viewer))).status_code == 403
    assert (await client.post(path)).status_code in {401, 403}
    assert (
        await client.post(f"/api/v1/schedules/{schedule.id}/resume", headers=make_auth_headers(user))
    ).status_code == 409
    response = await client.post(path, headers=make_auth_headers(user))
    assert response.status_code == 200, response.text
    assert response.json()["verification"] == "verified"
    assert (
        await client.post(f"/api/v1/schedules/{schedule.id}/resume", headers=make_auth_headers(user))
    ).status_code == 200
    assert drive.calls.count("upload_new") == 2


@pytest.mark.parametrize(
    "response",
    [
        {"files": [{"id": "one"}, {"id": "two"}]},
        {"files": [{"id": "one"}], "nextPageToken": "more"},
        {"files": [{"id": "one"}], "incompleteSearch": True},
    ],
)
async def test_real_drive_reader_refuses_nonunique_or_incomplete_search(monkeypatch, response):
    from unittest.mock import MagicMock

    client = delivery._GoogleDriveClient({}, None)
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = response
    monkeypatch.setattr(client, "_service", lambda: service)
    with pytest.raises(ValueError, match="ambiguous"):
        await client.find_unique(parent_id="folder", mime_type=delivery._PDF_MIME, app_properties={"schedule_id": "s"})
    request = service.files.return_value.list.call_args.kwargs
    assert request["pageSize"] == 2
    assert "sha256Checksum" in request["fields"]
    assert "'folder' in parents" in request["q"]
    service.files.return_value.create.assert_not_called()
    service.files.return_value.update.assert_not_called()


async def test_intent_is_durable_before_first_provider_request(db, monkeypatch):
    original = EvidenceDrive.find_folder
    observed = []

    async def check(self, **kwargs):
        intent = await db.scalar(select(AuditEvent).where(AuditEvent.action == "report.delivery.prepared"))
        assert intent is not None
        assert intent.payload["content"]["xlsx"]["sha256"]
        observed.append(intent.id)
        return await original(self, **kwargs)

    monkeypatch.setattr(EvidenceDrive, "find_folder", check)
    await uncertain_delivery(db, monkeypatch)
    assert len(observed) == 2


async def test_cancelled_run_keeps_cancellation_after_verified_delivery(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    job.status = "cancelled"
    schedule.is_active = False
    schedule.plan_version += 1
    await db.commit()
    assert (await reconcile(db, tenant, user, schedule, job))["verification"] == "verified"
    assert job.status == "cancelled"
    assert job.result_summary["reason"] == "blocked"
    assert not schedule.is_active
    assert schedule.paused_at


async def test_revoked_operator_cannot_read_evidence(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    user.is_active = False
    await db.commit()
    with pytest.raises(PermissionError):
        await reconcile(db, tenant, user, schedule, job)
    assert "read_evidence" not in drive.calls


async def test_revoked_connector_during_read_stays_uncertain(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    lookup = drive.find_unique

    async def revoke(**kwargs):
        found = await lookup(**kwargs)
        if (kwargs.get("app_properties") or {}).get("kind") == "xlsx":
            connector.is_enabled = False
            await db.flush()
        return found

    monkeypatch.setattr(drive, "find_unique", revoke)
    assert (await reconcile(db, tenant, user, schedule, job))["verification"] == "uncertain"


async def test_role_removed_during_read_cannot_use_cached_membership(db, monkeypatch):
    from app.models.user import UserRole

    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    lookup = drive.find_unique

    async def revoke(**kwargs):
        found = await lookup(**kwargs)
        if (kwargs.get("app_properties") or {}).get("kind") == "xlsx":
            await db.execute(UserRole.__table__.delete().where(UserRole.user_id == user.id))
        return found

    monkeypatch.setattr(drive, "find_unique", revoke)
    with pytest.raises(PermissionError):
        await reconcile(db, tenant, user, schedule, job)
    await db.refresh(job)
    assert job.result_summary["verification"] == "uncertain"


async def test_later_approved_occurrence_updates_attempt_without_duplicate_files(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    assert (await reconcile(db, tenant, user, schedule, job))["verification"] == "verified"
    old_markers = {r["appProperties"]["delivery_attempt"] for r in drive._files.values()}
    schedule.paused_at = None  # API resume gate independently tested above
    drive.lose_receipt = False
    await db.commit()
    later = await jobs.run_schedule_now(db, schedule.id, tenant_id=tenant.id, actor_id=user.id)
    assert later.reason == "done"
    assert len(drive._files) == 2
    assert drive.calls.count("upload_new") == 2
    assert drive.calls.count("update_existing") == 2
    assert not old_markers & {r["appProperties"]["delivery_attempt"] for r in drive._files.values()}


async def test_google_update_sends_attempt_marker_with_media(monkeypatch):
    from unittest.mock import MagicMock

    client = delivery._GoogleDriveClient({}, None)
    service = MagicMock()
    service.files.return_value.update.return_value.execute.return_value = {"id": "f"}
    monkeypatch.setattr(client, "_service", lambda: service)
    props = {"delivery_attempt": "a" * 64, "kind": "pdf"}
    await client.update_existing(file_id="f", content=b"pdf", mime_type=delivery._PDF_MIME, app_properties=props)
    sent = service.files.return_value.update.call_args.kwargs
    assert sent["body"] == {"appProperties": props}
    assert sent["media_body"].getbytes(0, 3) == b"pdf"


async def test_reconciliation_audit_contains_only_delivery_evidence(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch)
    assert (await reconcile(db, tenant, user, schedule, job))["verification"] == "verified"
    event = await db.scalar(
        select(AuditEvent).where(AuditEvent.job_id == job.id, AuditEvent.action == "jobs.run.reconciled")
    )
    assert set(event.payload["outputs"]) == {"upload"}


async def test_interrupted_delivery_and_readback_reconstruct_completed_query_usage(db, monkeypatch):
    tenant, user, schedule, job, connector, drive = await uncertain_delivery(db, monkeypatch, with_query=True)
    summary = {k: v for k, v in job.result_summary.items() if k != "usage"}
    job.result_summary = summary
    job.status = "running"
    await db.commit()
    assert await jobs._settle_interrupted(db, schedule, job) == "blocked"
    await db.refresh(job)
    assert job.result_summary["usage"]["bytes_scanned"] == 1024
    # Also repair an older uncertain run that already missed reconstruction.
    job.result_summary = {k: v for k, v in job.result_summary.items() if k != "usage"}
    await db.commit()
    before = list(drive.calls)
    result = await reconcile(db, tenant, user, schedule, job)
    assert result["verification"] == "verified"
    assert job.result_summary["usage"]["bytes_scanned"] == 1024
    assert job.result_summary["usage"]["usd"] is None
    assert set(drive.calls[len(before) :]) == {"read_evidence"}
    assert drive.calls.count("upload_new") == 2
