"""Reconcile scheduled-report query usage from bound provider metadata, never replay."""

import asyncio
import hashlib
import uuid

from sqlalchemy import select

from app.models.mcp_connector import McpConnector
from app.services import audit_service, bigquery_service
from app.services.jobs.report_queries import rebuild_query_usage


async def _source(db, tenant_id, intent):
    from app.mcp.tools.bigquery_tools import _extract_credentials
    from app.services.jobs.recovery import EvidenceUnavailableError

    source = await db.scalar(
        select(McpConnector)
        .where(
            McpConnector.id == uuid.UUID(intent["source_id"]),
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "bigquery",
            McpConnector.is_enabled.is_(True),
            McpConnector.status == "active",
        )
        .execution_options(populate_existing=True)
    )
    if (
        source is None
        or hashlib.sha256((source.encrypted_credentials or "").encode()).hexdigest() != intent["credential_binding"]
    ):
        raise EvidenceUnavailableError("Query source changed or unavailable")
    credentials, project, location = _extract_credentials(source)
    if project != intent["project_id"] or (location or "US") != intent["location"]:
        raise EvidenceUnavailableError("Query source project/location changed")
    return credentials


async def reconcile_queries(db, *, tenant_id, actor_id, job):
    """Caller holds the schedule execution lock. All-or-nothing, bounded reads."""
    from app.services.jobs.recovery import EvidenceUnavailableError, _authorize

    summary = dict(job.result_summary or {})
    original = list(summary.get("report_queries", []))
    if len(original) > 128:
        raise EvidenceUnavailableError("Query evidence exceeds recovery limit")
    updated = []
    async with asyncio.timeout(20):
        for intent in original:
            if intent.get("state") in {"complete", "not_dispatched"}:
                updated.append(intent)
                continue
            try:
                credentials = await _source(db, tenant_id, intent)
                found = await bigquery_service.read_query_receipt(
                    credentials, intent["project_id"], intent["provider_job_id"], location=intent["location"]
                )
            except Exception as exc:
                raise EvidenceUnavailableError("Query provider evidence unavailable") from exc
            if (
                found.get("state") != "DONE"
                or found.get("job_id") != intent["provider_job_id"]
                or any(
                    found.get(k) != intent[k]
                    for k in ("project_id", "location", "query_sha256", "maximum_bytes_billed")
                )
                or any(type(found.get(k)) is not int or found[k] < 0 for k in ("bytes_processed", "bytes_billed"))
                or found["bytes_billed"] > intent["maximum_bytes_billed"]
            ):
                raise EvidenceUnavailableError("Query provider evidence incomplete or mismatched")
            await _source(db, tenant_id, intent)  # recheck revocation after network read
            updated.append(
                {
                    **intent,
                    "state": "complete",
                    "bytes_processed": found["bytes_processed"],
                    "bytes_billed": found["bytes_billed"],
                    "cache_hit": found.get("cache_hit") is True,
                    "provider_failed": found.get("provider_failed") is True,
                    "reconciled": True,
                }
            )
    await _authorize(db, tenant_id, actor_id)
    await db.refresh(job, with_for_update=True)
    if (job.result_summary or {}).get("report_queries", []) != original or job.status == "running":
        raise EvidenceUnavailableError("Query evidence changed during recovery")
    # Cancellation may have changed other summary fields while provider reads ran.
    summary = dict(job.result_summary or {})
    summary["report_queries"] = updated
    usage = rebuild_query_usage(summary)
    job.result_summary = {**summary, "usage": usage}
    await audit_service.log_event(
        db,
        tenant_id=tenant_id,
        category="jobs",
        action="jobs.queries.reconciled",
        actor_id=actor_id,
        resource_type="job",
        resource_id=str(job.id),
        job_id=job.id,
        payload={
            "provider": "bigquery",
            "queries": len(updated),
            "replayed": False,
            "bytes_scanned": usage["bytes_scanned"],
        },
    )
    await db.commit()
    return job.result_summary
