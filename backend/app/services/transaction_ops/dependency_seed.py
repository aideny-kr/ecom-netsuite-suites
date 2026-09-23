"""Seed positive historical identities once before using change-only discovery.

No provider calls or finding edits. Every bounded batch commits with the runner's
leased progress checkpoint. The final audit receipt and last batch are atomic;
a crash before that checkpoint safely repeats the idempotent inserts.
"""

from uuid import UUID

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionConfig, TransactionFinding, TransactionRun
from app.services.transaction_ops import dependency_index
from app.services.transaction_ops import state_service as state


async def advance(db, tenant_id, config_id, checkpoint):
    config = await state.get_config(db, tenant_id, config_id)
    scope = state.business_digest(
        {
            "version": 1,
            "connection_id": config.netsuite_connection_id,
            "account_id": config.netsuite_account_id,
            "subsidiary_id": config.subsidiary_id,
        }
    )
    seeded = await db.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == "transaction_ops.dependency_index.seeded",
            AuditEvent.resource_type == TransactionConfig.__tablename__,
            AuditEvent.resource_id == str(config.id),
            AuditEvent.payload["scope_digest"].astext == scope,
        )
        .limit(1)
    )
    if seeded:
        return {"complete": True, "scope_digest": scope}
    if not isinstance(checkpoint, dict) or (checkpoint.get("scope_digest") not in (None, scope)):
        raise state.StateError("dependency_seed_scope_changed")
    f, r = TransactionFinding, TransactionRun
    query = (
        select(f, r)
        .join(r, (r.id == f.run_id) & (r.tenant_id == tenant_id))
        .where(
            f.tenant_id == tenant_id,
            r.config_id == config.id,
        )
    )
    after = UUID(checkpoint["after_id"]) if checkpoint.get("after_id") else UUID(int=0)
    through = (
        UUID(checkpoint["through_id"])
        if checkpoint.get("through_id")
        else await db.scalar(query.with_only_columns(f.id).order_by(f.id.desc()).limit(1))
    )
    rows = (
        []
        if through is None
        else (await db.execute(query.where(f.id > after, f.id <= through).order_by(f.id).limit(101))).all()
    )
    for finding, run in rows[:100]:
        await dependency_index.record_dependencies(db, tenant_id, run, finding)
    complete = len(rows) <= 100
    if complete:
        await state._audit(
            db,
            tenant_id,
            "dependency_index.seeded",
            config,
            payload={
                "scope_digest": scope,
                "version": 1,
                "evidence_use": "observed_positive_identities_only",
            },
        )
    return {
        "complete": complete,
        "scope_digest": scope,
        "through_id": str(through) if through else None,
        "after_id": str(rows[min(len(rows), 100) - 1][0].id) if rows else str(after),
        "observations_scanned": checkpoint.get("observations_scanned", 0) + min(len(rows), 100),
    }
