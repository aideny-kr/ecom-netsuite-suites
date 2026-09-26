"""Bounded maintenance of derived read metadata; reports and read clocks are unchanged."""

from uuid import UUID

from sqlalchemy import text

from app.core.database import set_tenant_context
from app.services import audit_service


async def backfill_batch(db, tenant_id: UUID, *, limit=500):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("batch limit must be between 1 and 1000")
    tenant_id = UUID(str(tenant_id))
    await set_tenant_context(db, tenant_id)
    await db.execute(text("SET LOCAL lock_timeout = '2s'"))
    await db.execute(text("SET LOCAL statement_timeout = '20s'"))
    result = await db.execute(
        text("""
            WITH batch AS (
                SELECT id FROM transaction_ops_findings
                WHERE tenant_id = :tenant_id AND review_metadata_json IS NULL
                ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED
            )
            UPDATE transaction_ops_findings AS finding
            SET review_metadata_json = public.transaction_review_metadata(finding.report_json)
            FROM batch
            WHERE finding.id = batch.id AND finding.tenant_id = :tenant_id
            RETURNING finding.id
        """),
        {"tenant_id": tenant_id, "limit": limit},
    )
    updated = len(result.scalars().all())
    remaining = await db.scalar(
        text("""
            SELECT count(*) FROM transaction_ops_findings
            WHERE tenant_id = :tenant_id AND review_metadata_json IS NULL
        """),
        {"tenant_id": tenant_id},
    )
    if updated:
        await audit_service.log_event(
            db,
            tenant_id,
            category="transaction_ops",
            action="review_metadata.backfilled",
            actor_type="system",
            resource_type="transaction_ops_findings",
            payload={"updated": updated, "remaining": remaining},
        )
    await db.commit()
    return {"updated": updated, "remaining": remaining}
