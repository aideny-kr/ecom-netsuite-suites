"""Operator provisioning of a previously verified schema/timestamp contract.

Not a model tool: schema identity and UTC-naive interpretation must first be
verified against the source API. Revisions preserve old evidence and pauses.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.tenant import Tenant
from app.models.transaction_ops import TransactionConfig, TransactionRun
from app.schemas.transaction_runs import ConfigCreate
from app.services.transaction_ops import metabase_reader
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.periods import ReconciliationPolicy


async def bind_verified_replica(db, tenant_id, config_ids, binding, *, actor, policy=None):
    await set_tenant_context(db, str(tenant_id))
    await state._human(db, tenant_id, actor, "connections.manage")
    binding = metabase_reader.ReplicaBinding.model_validate(binding)
    policy = ReconciliationPolicy.model_validate(policy or {})
    if not 1 <= len(config_ids) <= 20 or len(set(config_ids)) != len(config_ids):
        raise state.StateError("invalid_config_scope", 422)
    # Same tenant lock as create_config serializes idempotent revisions.
    await db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    connector = await metabase_reader._connector(db, tenant_id, binding)
    prepared = []
    for identifier in config_ids:
        old = await state.get_config(db, tenant_id, identifier, lock=True)
        mapping = {
            **old.mapping_json,
            "metabase_replica": binding.model_dump(mode="json"),
            "reconciliation_policy": policy.model_dump(mode="json"),
        }
        existing = await db.scalar(
            select(TransactionConfig).where(
                TransactionConfig.tenant_id == tenant_id, TransactionConfig.supersedes_config_id == old.id
            )
        )
        if existing:
            if existing.mapping_json != mapping:
                raise state.StateError("config_already_revised", 409)
            prepared.append((old, existing, None))
            continue
        if old.mapping_json == mapping:
            prepared.append((old, old, None))
            continue
        active = await db.scalar(
            select(TransactionRun.id)
            .where(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.config_id == old.id,
                TransactionRun.status.in_(["pending", "running"]),
            )
            .limit(1)
        )
        if active:
            raise state.StateError("config_has_active_runs", 409)
        TransactionMapping.model_validate(mapping)
        values = {name: getattr(old, name) for name in ConfigCreate.model_fields}
        values["mapping_json"] = mapping
        request = ConfigCreate.model_validate(values)
        await state._check_bindings(db, tenant_id, request)
        # Include predecessor in identity: a revision cannot silently reuse a
        # differently owned lifecycle (including another paused config).
        key = state.business_digest({"supersedes": old.id, **request.model_dump(mode="json")})
        new = TransactionConfig(
            tenant_id=tenant_id,
            config_key=key,
            supersedes_config_id=old.id,
            enabled=old.enabled,
            created_by=actor.id,
            **request.model_dump(),
        )
        prepared.append((old, new, request))
    now = datetime.now(timezone.utc)
    try:
        await metabase_reader.read_order_page(
            db, tenant_id, binding.model_dump(mode="json"), now - timedelta(days=1), now, page_size=1, now=now
        )
    except Exception:
        raise state.StateError("replica_verification_failed", 422) from None
    for old, new, request in prepared:
        if request is not None:
            db.add(new)
            old.enabled = False
            old.schedule_enabled = False
            await db.flush()
            await state._audit(
                db,
                tenant_id,
                "config.revise",
                new,
                actor,
                {"supersedes_config_id": str(old.id), "reason": "verified_metabase_replica"},
            )
    connector.metadata_json = {
        **(connector.metadata_json or {}),
        "transaction_replica": binding.model_dump(mode="json"),
    }
    await state._audit(
        db,
        tenant_id,
        "replica.bind",
        connector,
        actor,
        {"config_ids": [str(new.id) for _, new, _ in prepared], "timestamp_storage": binding.timestamp_storage},
    )
    await state._commit(db, tenant_id)
    return [new for _, new, _ in prepared]
