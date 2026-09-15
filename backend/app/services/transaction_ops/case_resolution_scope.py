"""Persisted human restrictions narrow a case; they never authorize a write."""

from uuid import UUID

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionCase

ACTION = "accounting.resolution_scope.requested"


def _target(value, *, mutation=False):
    if not isinstance(value, dict):
        raise ValueError("invalid_case_resolution_scope")
    fields = ("kind", "record_type", "record_id") if mutation else ("record_type", "record_id")
    if set(value) != set(fields) or any(not isinstance(value[k], str) or not value[k] for k in fields):
        raise ValueError("invalid_case_resolution_scope")
    if not value["record_id"].isdigit():
        raise ValueError("invalid_case_resolution_scope")
    return {k: value[k] for k in fields}


async def load(db, tenant_id, case_id):
    await set_tenant_context(db, str(tenant_id))
    event = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.category == "transaction_ops",
            AuditEvent.action == ACTION,
            AuditEvent.resource_type == "transaction_case",
            AuditEvent.resource_id == str(case_id),
            AuditEvent.actor_type == "user",
            AuditEvent.actor_id.is_not(None),
            AuditEvent.status == "success",
        )
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return None
    case = await db.scalar(
        select(TransactionCase).where(TransactionCase.tenant_id == tenant_id, TransactionCase.id == UUID(str(case_id)))
    )
    p = event.payload or {}
    if (
        not case
        or p.get("schema_version") != 1
        or p.get("account_id") != case.scope_json.get("netsuite_account_id")
        or p.get("order_reference") != case.order_reference
        or not isinstance(p.get("allowed_mutations"), list)
        or not 1 <= len(p["allowed_mutations"]) <= 20
        or not isinstance(p.get("preserve_records"), list)
        or len(p["preserve_records"]) > 20
    ):
        raise ValueError("invalid_case_resolution_scope")
    allowed = [_target(v, mutation=True) for v in p["allowed_mutations"]]
    preserved = [_target(v) for v in p["preserve_records"]]
    if any({k: a[k] for k in ("record_type", "record_id")} in preserved for a in allowed):
        raise ValueError("conflicting_case_resolution_scope")
    return {"audit_id": str(event.id), "allowed_mutations": allowed, "preserve_records": preserved}


def allows(scope, proposal):
    if scope is None:
        return True
    return {k: proposal.get(k) for k in ("kind", "record_type", "record_id")} in scope["allowed_mutations"]


async def validate(db, tenant_id, proposal):
    current = await load(db, tenant_id, proposal["case_id"])
    if current != proposal.get("resolution_scope"):
        raise ValueError("case_resolution_scope_changed_refresh_approval")
    if not allows(current, proposal):
        raise ValueError("outside_case_resolution_scope")
