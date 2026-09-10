"""Transactions entry points reuse durable investigations and backend-owned scopes."""

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.canonical import Order
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service
from app.services.transaction_ops.framework_defaults import ensure_framework_configs
from app.services.transaction_ops.normalization import source_entity_key


async def source_configs(db, user, source_id):
    await state_service._human(db, user.tenant_id, user, "recon.run")
    configs = [
        row for row in await state_service.list_configs(db, user.tenant_id) if row.source_connection_id == source_id
    ]
    if not configs:
        configs = await ensure_framework_configs(db, user.tenant_id, source_id, actor=user)
    return configs


async def investigate_order(db, user, order_id, evaluation_key):
    await set_tenant_context(db, user.tenant_id)
    await state_service._human(db, user.tenant_id, user, "recon.run")
    order = await db.scalar(select(Order).where(Order.id == order_id, Order.tenant_id == user.tenant_id))
    if order is None:
        raise state_service.StateError("order_not_found", 404)
    if order.source != "solidus" or order.source_connection_id is None:
        raise state_service.StateError("order_source_unavailable", 422)
    entity = source_entity_key((order.raw_data or {}).get("order") or {})
    configs = await source_configs(db, user, order.source_connection_id)
    scopes = [
        row
        for row in configs
        if (row.mapping_json.get("business_entity_subsidiaries") or {}).get(entity) == row.subsidiary_id
    ]
    if len(scopes) != 1:
        raise state_service.StateError("order_scope_unavailable", 422)
    return await state_service.create_run(
        db,
        user.tenant_id,
        scopes[0].id,
        RunCreate(evaluation_key=str(evaluation_key), order_references=(order.order_number,)),
        actor=user,
    )


async def reconcile_source(db, user, source_id, request):
    configs = await source_configs(db, user, source_id)
    enabled = [row for row in configs if row.enabled]
    if not enabled:
        raise state_service.StateError("config_disabled", 409)
    return [await state_service.create_run(db, user.tenant_id, row.id, request, actor=user) for row in enabled]
