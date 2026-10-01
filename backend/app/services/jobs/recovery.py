"""Operator-triggered, read-only reconciliation of fully delivered scheduled reports.

No generic uncertainty override, provider write, model retry, or step replay.
Absent, legacy, partial, stale and ambiguous evidence remains fenced.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.audit import AuditEvent
from app.models.job import Job
from app.models.mcp_connector import McpConnector
from app.models.pipeline import Schedule
from app.models.tenant import Tenant
from app.models.user import Permission, RolePermission, User, UserRole
from app.services import audit_service
from app.services.jobs.registry import STEP_REGISTRY, schedule_delivery_identity
from app.services.report import report_delivery as delivery


class EvidenceUnavailableError(ValueError):
    pass


async def _authorize(db, tenant_id, actor_id):
    await set_tenant_context(db, str(tenant_id))
    actor = await db.scalar(
        select(User.id).where(
            User.id == actor_id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    active = await db.scalar(select(Tenant.is_active).where(Tenant.id == tenant_id))
    allowed = await db.scalar(
        select(Permission.id)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == actor_id, UserRole.tenant_id == tenant_id, Permission.codename == "schedules.manage")
        .limit(1)
    )
    if not actor or not active or not allowed:
        raise PermissionError("Current company schedules.manage permission required")


async def _connector(db, tenant_id, intent):
    connector = await db.scalar(
        select(McpConnector)
        .where(
            McpConnector.id == uuid.UUID(intent["connector_id"]),
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "google_sheets",
            McpConnector.is_enabled.is_(True),
            McpConnector.status == "active",
        )
        .execution_options(populate_existing=True)
    )
    if (
        connector is None
        or hashlib.sha256((connector.encrypted_credentials or "").encode()).hexdigest() != intent["credential_binding"]
        or (connector.metadata_json or {}).get("shared_drive_id") != intent["shared_drive_id"]
    ):
        raise EvidenceUnavailableError("delivery connector changed or unavailable")
    return connector


async def _read_unique(client, **kwargs):
    try:
        return await client.find_unique(**kwargs)
    except Exception as exc:
        raise EvidenceUnavailableError("provider evidence unavailable") from exc


async def _verify_delivery(db, tenant_id, schedule_id, parameters, step, receipts, events):
    prepared = [
        e for e in events if e.action == "report.delivery.prepared" and (e.payload or {}).get("step_id") == step["id"]
    ]
    if len(prepared) != 1:
        raise EvidenceUnavailableError("missing or ambiguous content-bound intent")
    event = prepared[0]
    intent = event.payload
    report_step = step["params"]["report_step"]
    identity = schedule_delivery_identity(schedule_id, report_step)
    if (
        event.resource_id != receipts.get(report_step, {}).get("report_id")
        or intent["period_key"] != parameters["period_key"]
        or intent["folder_properties"] != identity.folder_props
        or intent["file_properties"] != {**identity.file_props, "period_key": parameters["period_key"]}
        or intent["lock_key"] != identity.lock_key
    ):
        raise EvidenceUnavailableError("delivery intent does not match the saved operation")
    connector = await _connector(db, tenant_id, intent)
    try:
        credentials = decrypt_credentials(connector.encrypted_credentials)
    except Exception as exc:
        raise EvidenceUnavailableError("delivery credentials unavailable") from exc
    client = delivery._build_drive_client(
        credentials.get("service_account_json", credentials), intent["shared_drive_id"]
    )
    # Same lock as delivery, across all read-back checks until local settlement.
    # Manual redelivery cannot overwrite the bytes between inspection and commit.
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": identity.lock_key}):
        raise EvidenceUnavailableError("delivery is still in progress")
    root = await _read_unique(
        client, parent_id=intent["shared_drive_id"], name="Reports", mime_type="application/vnd.google-apps.folder"
    )
    if not root:
        raise EvidenceUnavailableError("delivery folder not found; absence is not proof of no effect")
    folder = await _read_unique(
        client,
        parent_id=root["id"],
        app_properties=identity.folder_props,
        mime_type="application/vnd.google-apps.folder",
    )
    if not folder:
        raise EvidenceUnavailableError("report folder not found")
    artifact = {"folder_id": folder["id"], "period_key": parameters["period_key"]}
    for kind, mime in (("pdf", delivery._PDF_MIME), ("xlsx", delivery._XLSX_MIME)):
        expected = intent["content"][kind]
        properties = {**intent["file_properties"], "kind": kind}
        attempt = delivery.delivery_attempt_key(
            tenant_id, event.job_id, step["id"], event.resource_id, parameters["period_key"]
        )
        if intent.get("delivery_attempt") != attempt:
            raise EvidenceUnavailableError("missing or mismatched delivery attempt")
        found = await _read_unique(client, parent_id=folder["id"], app_properties=properties, mime_type=mime)
        if (
            not found
            or not found.get("id")
            or expected["mime"] != mime
            or len(expected["sha256"]) != 64
            or found.get("sha256Checksum") != expected["sha256"]
            or found.get("appProperties", {}).get("delivery_attempt") != attempt
            or found.get("size") != str(expected["size"])
            or found.get("mimeType") != mime
            or folder["id"] not in found.get("parents", [])
            or any(found.get("appProperties", {}).get(k) != v for k, v in properties.items())
        ):
            raise EvidenceUnavailableError("provider content missing, stale or mismatched")
        artifact[f"{kind}_file_id"] = found["id"]
        # Construct a normal Drive link; no provider-supplied arbitrary URL.
        artifact[f"{kind}_url"] = f"https://drive.google.com/file/d/{found['id']}/view"
    await _connector(db, tenant_id, intent)  # revoke/change during the read-back
    return artifact


async def reconcile_run(db, *, tenant_id, schedule_id, job_id, actor_id):
    await _authorize(db, tenant_id, actor_id)
    schedule = await db.scalar(select(Schedule).where(Schedule.id == schedule_id, Schedule.tenant_id == tenant_id))
    job = await db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.tenant_id == tenant_id,
            Job.job_type == "scheduled_job",
            Job.parameters["schedule_id"].astext == str(schedule_id),
        )
    )
    if schedule is None or job is None:
        raise LookupError("Scheduled run not found")
    key = int.from_bytes(
        hashlib.sha256(f"schedule:{tenant_id}:{schedule_id}".encode()).digest()[:8], "big", signed=True
    )
    engine = db.bind.engine if isinstance(db.bind, AsyncConnection) else db.bind
    async with engine.connect() as lock:
        if not await lock.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
            return {"verification": "uncertain", "detail": "schedule execution or reconciliation in progress"}
        try:
            await db.refresh(job)
            if (job.result_summary or {}).get("verification") == "verified":
                return {"verification": "verified", "job_id": str(job_id)}
            if (job.result_summary or {}).get("verification") != "uncertain" or job.status == "running":
                raise EvidenceUnavailableError("run is not awaiting reconciliation")
            summary = dict(job.result_summary or {})
            from app.services.jobs.report_queries import queries_uncertain

            if queries_uncertain(summary):
                raise EvidenceUnavailableError("Report query usage is unknown; provider-specific recovery required")
            parameters = job.parameters or {}
            steps = (parameters.get("plan") or {}).get("steps") or []
            receipts = summary.get("step_receipts", {})
            events = (
                await db.scalars(
                    select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.job_id == job_id)
                )
            ).all()
            effects = [e for e in events if e.action.endswith(".started") and e.category == "jobs"]
            writes = [step for step in steps if step.get("type") == "drive.upload"]
            if (
                not writes
                or len({step["id"] for step in steps}) != len(steps)
                or any(
                    e.action != "drive.upload.started" or e.resource_id not in {s["id"] for s in writes}
                    for e in effects
                )
                or len(effects) != len(writes)
            ):
                raise EvidenceUnavailableError(
                    "unrecognized or unaccounted effect; provider-specific recovery required"
                )
            for step in steps:
                spec = STEP_REGISTRY.get(step.get("type"))
                if step not in writes and (not spec or spec.kind != "read" or step["id"] not in receipts):
                    raise EvidenceUnavailableError("unexecuted step or missing durable step receipt")
            outputs = dict(receipts)
            # A bounded read-only recovery operation, separate from execution's
            # exhausted budget. No remaining execution budget is reset or spent.
            async with asyncio.timeout(20):
                for step in writes:
                    outputs[step["id"]] = await _verify_delivery(
                        db, tenant_id, schedule_id, parameters, step, receipts, events
                    )
            await _authorize(db, tenant_id, actor_id)
            await db.refresh(job, with_for_update=True)
            await db.refresh(schedule, with_for_update=True)
            cancelled = job.status == "cancelled"
            job.result_summary = {
                **summary,
                "verification": "verified",
                "execution_complete": True,
                "reason": "blocked" if cancelled else "done",
                "outputs": outputs,
                "detail": "Recorded report content verified by provider read-back; no step replayed",
            }
            if not cancelled:
                job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            schedule.last_run_status = job.result_summary["reason"]
            await audit_service.log_event(
                db,
                tenant_id=tenant_id,
                category="jobs",
                action="jobs.run.reconciled",
                actor_id=actor_id,
                resource_type="job",
                resource_id=str(job_id),
                job_id=job_id,
                payload={
                    "verification": "verified",
                    "source": "drive_content_readback",
                    "replayed": False,
                    "outputs": {step["id"]: outputs[step["id"]] for step in writes},
                },
            )
            await db.commit()
            return {"verification": "verified", "job_id": str(job_id)}
        except PermissionError:
            await db.rollback()
            await set_tenant_context(db, str(tenant_id))
            await audit_service.log_event(
                db,
                tenant_id=tenant_id,
                category="jobs",
                action="jobs.run.reconciliation_denied",
                actor_id=actor_id,
                resource_type="job",
                resource_id=str(job_id),
                job_id=job_id,
                payload={"verification": "uncertain", "replayed": False},
                status="error",
            )
            await db.commit()
            raise
        except (EvidenceUnavailableError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            await db.rollback()
            await set_tenant_context(db, str(tenant_id))
            await audit_service.log_event(
                db,
                tenant_id=tenant_id,
                category="jobs",
                action="jobs.run.reconciliation_pending",
                actor_id=actor_id,
                resource_type="job",
                resource_id=str(job_id),
                job_id=job_id,
                payload={"verification": "uncertain", "detail": type(exc).__name__, "replayed": False},
                status="error",
            )
            await db.commit()
            return {
                "verification": "uncertain",
                "detail": "Evidence unavailable or incomplete; operation remains fenced",
            }
        finally:
            await lock.rollback()
