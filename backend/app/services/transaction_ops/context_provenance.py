"""Versioned accounting context on the existing append-only tenant audit store.

Config-row locking serializes revisions; optimistic versions reject stale review.
No context write modifies executable profiles, company instructions or soul files.
"""

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.tenant import Tenant
from app.schemas.accounting_context import ContextDecision, ContextDraft, ContextScope
from app.services.audit_service import log_event
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_profiles import NAMESPACE, config_scope
from app.services.transaction_ops.netsuite_reader import _account

ACTION = "accounting.context.version"
AUTHORITY = (
    "Reviewed context is advisory evidence, not tool instructions or financial approval. "
    "It cannot enable a treatment, change a profile, override native evidence, or authorize a write. "
    "Sources are supplied evidence references; this store does not certify their external contents."
)


def _query(tenant_id, config_id):
    return select(AuditEvent).where(
        AuditEvent.tenant_id == tenant_id,
        AuditEvent.action == ACTION,
        AuditEvent.resource_type == "transaction_ops_configs",
        AuditEvent.resource_id == str(config_id),
    )


async def _latest(db, tenant_id, config_id):
    return await db.scalar(
        _query(tenant_id, config_id).order_by(AuditEvent.payload["version"].as_integer().desc()).limit(1)
    )


async def _binding(db, tenant_id, config):
    row = (
        await db.execute(
            select(Connection.metadata_json, Connection.status, Connection.auth_type).where(
                Connection.id == config.netsuite_connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
            )
        )
    ).one_or_none()
    tenant_active = await db.scalar(select(Tenant.is_active).where(Tenant.id == tenant_id))
    metadata = row.metadata_json or {} if row else {}
    try:
        account = _account(metadata.get("account_id", ""))
    except ValueError:
        account = None
    if (
        not tenant_active
        or not config.enabled
        or not row
        or row.status not in ACTIVE_CONNECTION_STATUSES
        or account != _account(config.netsuite_account_id)
    ):
        return None
    # Exclude rotating OAuth tokens. A changed source/treatment binding requires review again.
    return state.business_digest(
        {
            "tenant_id": str(tenant_id),
            "config_id": str(config.id),
            "connection_id": str(config.netsuite_connection_id),
            "scope": config_scope(config),
            "mapping": config.mapping_json,
            "profiles": metadata.get(NAMESPACE),
            "auth_type": row.auth_type,
        }
    )


async def _authorize(db, tenant_id, config_id, actor, *, write=False):
    await set_tenant_context(db, str(tenant_id))
    await state._human(db, tenant_id, actor, "connections.manage" if write else "recon.run")
    config = await state.get_config(db, tenant_id, config_id, lock=write)
    if not await db.scalar(select(Tenant.is_active).where(Tenant.id == tenant_id)):
        raise state.StateError("context_unavailable", 409)
    return config


def _entry_status(entry, binding, now):
    if entry["status"] == "invalidated":
        return "invalidated"
    if binding is None:
        return "connection_unavailable"
    if binding != entry["binding_sha256"]:
        return "revalidation_required"
    if now >= datetime.fromisoformat(entry["content"]["review_by"]):
        return "stale"
    if now < datetime.fromisoformat(entry["content"]["effective_from"]):
        return "not_yet_effective"
    return entry["status"]


def _project(event, binding, scope=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    entries = deepcopy(event.payload["entries"]) if event else {}
    selected = []
    for key, entry in entries.items():
        status = _entry_status(entry, binding, now)
        content = entry["content"]
        exact = scope is not None and content["scope"] == scope
        selected.append(
            {
                "key": key,
                "content_sha256": entry["content_sha256"],
                "revision": entry["revision"],
                "scope": content["scope"],
                "kind": content["kind"],
                "topic": content["topic"],
                "owner": content["owner"],
                "effective_from": content["effective_from"],
                "review_by": content["review_by"],
                "review": entry.get("review"),
                "status": status,
                "scope_match": exact,
                "usable_as_policy": exact and content["kind"] == "company_policy" and status == "approved",
                # Inferences/drafts remain inspectable only in the explicitly selected scope.
                "content": content if exact else None,
            }
        )
    # Different claims for the same topic/scope are a conflict, never last-writer-wins.
    for item in selected:
        if item["status"] not in {"approved", "verified"}:
            continue
        peers = [
            other
            for other in selected
            if other["scope"] == item["scope"]
            and other["topic"] == item["topic"]
            and other["status"] in {"approved", "verified"}
        ]
        if len(peers) > 1:
            for other in peers:
                other["status"] = "conflict"
                other["usable_as_policy"] = False
    return {
        "version": event.payload["version"] if event else 0,
        "audit_id": str(event.id) if event else None,
        "scope_required": scope is None,
        "entries": selected,
        "authority": AUTHORITY,
        "financial_writes": 0,
        "financial_approval": None,
    }


async def read_context(db, tenant_id, config_id, *, actor, scope: ContextScope | None = None):
    config = await _authorize(db, tenant_id, config_id, actor)
    return await context_manifest(db, tenant_id, config, scope=scope)


async def context_manifest(db, tenant_id, config, *, scope: ContextScope | None = None):
    """Internal read for already-authorized investigations; tenant is always explicit."""
    await set_tenant_context(db, str(tenant_id))
    if config.tenant_id != tenant_id:
        raise state.StateError("context_unavailable", 404)
    current = await state.get_config(db, tenant_id, config.id)
    binding = await _binding(db, tenant_id, current)
    if binding is None:
        return {"status": "unavailable", "entries": [], "authority": AUTHORITY}
    result = _project(await _latest(db, tenant_id, current.id), binding, scope.model_dump() if scope else None)
    return {**result, "config_id": str(current.id), "company_scope": config_scope(current), "binding_sha256": binding}


async def _append(db, tenant_id, config, actor, latest, entries, operation):
    version = latest.payload["version"] + 1 if latest else 1
    event = await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action=ACTION,
        actor_id=actor.id,
        resource_type="transaction_ops_configs",
        resource_id=str(config.id),
        payload={
            "schema_version": 1,
            "version": version,
            "parent_audit_id": str(latest.id) if latest else None,
            "entries": entries,
            "operation": operation,
            "financial_writes": 0,
            "financial_approval": None,
        },
    )
    await state._commit(db, tenant_id)
    return {"version": version, "audit_id": str(event.id), "financial_writes": 0, "financial_approval": None}


def _version(latest, expected):
    if expected != (latest.payload["version"] if latest else 0):
        raise state.StateError("context_version_changed", 409)


async def propose_context(db, tenant_id, config_id, request: ContextDraft, *, actor):
    config = await _authorize(db, tenant_id, config_id, actor, write=True)
    binding = await _binding(db, tenant_id, config)
    now = datetime.now(timezone.utc)
    if binding is None:
        raise state.StateError("context_unavailable", 409)
    if request.review_by <= now or any(s.observed_at > now for s in request.sources):
        raise state.StateError("context_source_or_review_date_invalid", 422)
    latest = await _latest(db, tenant_id, config.id)
    _version(latest, request.expected_version)
    entries = deepcopy(latest.payload["entries"]) if latest else {}
    if request.key not in entries and len(entries) >= 100:
        raise state.StateError("context_entry_limit", 409)
    content = request.model_dump(mode="json", exclude={"expected_version", "key"})
    prior = entries.get(request.key)
    # Reclassification must create a separately attributable proposal, never promote an inference.
    if prior and prior["content"]["kind"] != request.kind:
        raise state.StateError("context_kind_immutable", 409)
    entries[request.key] = {
        "content": content,
        "content_sha256": state.business_digest(content),
        "binding_sha256": binding,
        "revision": (prior["revision"] + 1) if prior else 1,
        "status": "inference" if request.kind == "inference" else "review_required",
        "created_by": str(actor.id),
        "created_at": now.isoformat(),
        "review": None,
    }
    return await _append(db, tenant_id, config, actor, latest, entries, {"kind": "propose", "key": request.key})


async def decide_context(db, tenant_id, config_id, key, request: ContextDecision, *, actor):
    config = await _authorize(db, tenant_id, config_id, actor, write=True)
    binding = await _binding(db, tenant_id, config)
    latest = await _latest(db, tenant_id, config.id)
    _version(latest, request.expected_version)
    entries = deepcopy(latest.payload["entries"]) if latest else {}
    entry = entries.get(key)
    if entry is None:
        raise state.StateError("context_not_found", 404)
    if request.content_sha256 != entry["content_sha256"]:
        raise state.StateError("context_content_changed", 409)
    now = datetime.now(timezone.utc)
    if request.decision == "approve":
        if entry["content"]["kind"] == "inference":
            raise state.StateError("inference_is_not_policy", 409)
        if _entry_status(entry, binding, now) != "review_required":
            raise state.StateError("context_new_revision_required", 409)
        for other_key, other in entries.items():
            if (
                other_key != key
                and other["content"]["scope"] == entry["content"]["scope"]
                and other["content"]["topic"] == entry["content"]["topic"]
                and _entry_status(other, binding, now) in {"approved", "verified"}
            ):
                raise state.StateError("context_conflicting_source", 409)
        entry["status"] = "approved" if entry["content"]["kind"] == "company_policy" else "verified"
    else:
        entry["status"] = "invalidated"
    entry["review"] = {
        "actor_id": str(actor.id),
        "at": now.isoformat(),
        **request.model_dump(exclude={"expected_version"}),
    }
    return await _append(db, tenant_id, config, actor, latest, entries, {"kind": request.decision, "key": key})


async def context_history(db, tenant_id, config_id, *, actor, before_version=None, limit=50):
    await _authorize(db, tenant_id, config_id, actor)
    query = _query(tenant_id, config_id)
    if before_version is not None:
        query = query.where(AuditEvent.payload["version"].as_integer() < before_version)
    events = list(await db.scalars(query.order_by(AuditEvent.payload["version"].as_integer().desc()).limit(limit + 1)))
    return {
        "versions": [
            {"audit_id": str(e.id), "actor_id": str(e.actor_id), "at": e.timestamp.isoformat(), **e.payload}
            for e in events[:limit]
        ],
        "next_before_version": events[limit - 1].payload["version"] if len(events) > limit else None,
        "authority": AUTHORITY,
    }
