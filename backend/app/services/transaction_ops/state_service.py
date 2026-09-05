"""Persistence choke points. This module never calls a provider or invokes an LLM.

Every spend and operation claim commits before returning. A runner must reserve
its budget before its next call and dispatch only a returned ClaimedOperation.
An uncertain or interrupted operation can only be reconciled, never reacquired.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.models.celigo import CeligoFlow, CeligoFlowStep
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.tenant import Tenant
from app.models.transaction_ops import (
    TransactionConfig,
    TransactionFinding,
    TransactionOperation,
    TransactionProposal,
    TransactionRun,
)
from app.models.user import Permission, RolePermission, User, UserRole
from app.schemas.transaction_runs import (
    ClaimedOperation,
    ConfigControl,
    ConfigCreate,
    ConfigOut,
    FindingReport,
    OperationReadPermit,
    ProgressUpdate,
    ProposalCreate,
    ProposalDecision,
    RunCreate,
    Termination,
    _bounded_json,
)
from app.services import audit_service
from app.services.feature_flag_service import get_all_flags
from app.services.transaction_ops.normalization import TransactionMapping

_LEASE = timedelta(seconds=180)
_EVIDENCE_AGE = timedelta(minutes=15)
_OPERATION_CALLS = 96
_OPERATION_TIME = timedelta(seconds=300)
_LEDGER_RESULT_KEYS = frozenset(
    {"dispatch_reserved", "provider", "payload_fingerprint", "dispatch_reserved_at", "termination_reason"}
)


class StateError(ValueError):
    def __init__(self, code: str, http_status: int = 409):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _clock(now=None):
    value = now or datetime.now(timezone.utc)
    if value.utcoffset() is None:
        raise ValueError("An aware clock is required")
    return value


def business_digest(value) -> str:
    def normalize(item):
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Nonfinite business key")
            # Avoid normalize() inheriting the caller's Decimal precision.
            with localcontext() as ctx:
                ctx.prec = max(60, len(item.as_tuple().digits))
                return format(item.normalize(), "f") if item else "0"
        if isinstance(item, datetime):
            return _clock(item).astimezone(timezone.utc).isoformat()
        if isinstance(item, uuid.UUID):
            return str(item)
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(val) for val in item]
        if isinstance(item, float):
            raise ValueError("Binary floats cannot identify financial work")
        return item

    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _one(db, tenant_id, model, identifier, *, lock=False):
    await set_tenant_context(db, str(tenant_id))
    query = (
        select(model)
        .where(model.tenant_id == tenant_id, model.id == identifier)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise StateError("not_found", 404)
    return row


async def _commit(db, tenant_id):
    await db.commit()
    # SET LOCAL is cleared by a real COMMIT. Never reuse a pooled session without
    # restoring the scope, including when the caller invokes another service.
    await set_tenant_context(db, str(tenant_id))


async def _audit(db, tenant_id, action, row, actor=None, payload=None):
    await audit_service.log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action=f"transaction_ops.{action}",
        actor_id=actor.id if actor else None,
        actor_type="user" if actor else "system",
        resource_type=row.__tablename__,
        resource_id=str(row.id),
        payload=payload,
    )


async def _human(db, tenant_id, actor, permission):
    if actor is None or actor.tenant_id != tenant_id:
        raise StateError("human_actor_required", 403)
    current = (
        await db.execute(
            select(User)
            .where(
                User.id == actor.id,
                User.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current is None or not current.is_active or current.actor_type != "user":
        raise StateError("human_actor_required", 403)
    # Read permission grants from the database on EVERY check. A User ORM
    # object's cached user_roles collection can outlive a revoked assignment.
    granted = (
        await db.execute(
            select(Permission.id)
            .join(
                RolePermission,
                RolePermission.permission_id == Permission.id,
            )
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(
                UserRole.tenant_id == tenant_id,
                UserRole.user_id == current.id,
                Permission.codename == permission,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if granted is None:
        raise StateError("permission_denied", 403)


async def _check_bindings(db, tenant_id, request):
    # These are local ownership/lifecycle checks. The root runner independently
    # validates live Framework/Celigo/provider configuration before any read/write.
    for step_id in filter(None, (request.source_step_id, request.target_step_id)):
        query = (
            select(CeligoFlowStep.id)
            .join(
                CeligoFlow,
                and_(
                    CeligoFlow.id == CeligoFlowStep.flow_id,
                    CeligoFlow.tenant_id == tenant_id,
                    CeligoFlow.celigo_connection_id == CeligoFlowStep.celigo_connection_id,
                ),
            )
            .join(
                Connection,
                and_(
                    Connection.id == CeligoFlowStep.celigo_connection_id,
                    Connection.tenant_id == tenant_id,
                    Connection.provider == "celigo",
                    Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
                ),
            )
            .where(CeligoFlowStep.id == step_id, CeligoFlowStep.tenant_id == tenant_id)
        )
        if (await db.execute(query)).scalar_one_or_none() is None:
            raise StateError("source_unavailable", 422)
    target = (
        await db.execute(
            select(Connection.id).where(
                Connection.id == request.netsuite_connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise StateError("destination_unavailable", 422)


async def get_config(db, tenant_id, config_id, *, lock=False):
    return await _one(db, tenant_id, TransactionConfig, config_id, lock=lock)


async def list_configs(db, tenant_id, *, scheduled_only=False):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionConfig).where(TransactionConfig.tenant_id == tenant_id)
    if scheduled_only:
        query = query.where(TransactionConfig.enabled.is_(True), TransactionConfig.schedule_enabled.is_(True))
    return list((await db.execute(query.order_by(TransactionConfig.created_at).limit(200))).scalars())


async def create_config(db: AsyncSession, tenant_id, request: ConfigCreate, *, actor):
    await set_tenant_context(db, str(tenant_id))
    await _human(db, tenant_id, actor, "connections.manage")
    try:
        TransactionMapping.model_validate(request.mapping_json)
    except ValidationError:
        raise StateError("invalid_mapping", 422) from None
    await _check_bindings(db, tenant_id, request)
    values = request.model_dump(mode="json")
    key = business_digest({k: v for k, v in values.items() if k not in {"name", "schedule_enabled"}})
    # Serialize creates by tenant without a global lock or caller-provided key.
    await db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    existing = (
        await db.execute(
            select(TransactionConfig).where(
                TransactionConfig.tenant_id == tenant_id,
                TransactionConfig.config_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        await _commit(db, tenant_id)
        return existing
    row = TransactionConfig(tenant_id=tenant_id, config_key=key, created_by=actor.id, **request.model_dump())
    db.add(row)
    await db.flush()
    await _audit(db, tenant_id, "config.create", row, actor)
    await _commit(db, tenant_id)
    return row


async def control_config(db, tenant_id, config_id, request: ConfigControl, *, actor):
    row = await get_config(db, tenant_id, config_id, lock=True)
    await _human(db, tenant_id, actor, "connections.manage")
    if request.schedule_enabled and not request.enabled:
        raise StateError("disabled_config_cannot_schedule", 422)
    row.enabled, row.schedule_enabled = request.enabled, request.schedule_enabled
    await _audit(db, tenant_id, "config.control", row, actor, request.model_dump())
    await _commit(db, tenant_id)
    return row


async def create_run(db, tenant_id, config_id, request: RunCreate, *, actor=None, now=None, resume_from_run_id=None):
    now = _clock(now)
    config = await get_config(db, tenant_id, config_id, lock=True)
    if not config.enabled:
        raise StateError("config_disabled")
    if request.origin == "schedule":
        if not config.schedule_enabled:
            raise StateError("schedule_disabled")
    else:
        await _human(db, tenant_id, actor, "recon.run")
    params = request.model_dump(mode="json")
    key = business_digest({"config": config.config_key, "params": request.model_dump()})
    existing = (
        await db.execute(
            select(TransactionRun).where(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.work_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        await _commit(db, tenant_id)
        return existing
    initial_progress = {}
    if resume_from_run_id is not None:
        previous = await get_run(db, tenant_id, resume_from_run_id)
        previous_scope = {k: v for k, v in previous.params_json.items() if k not in {"evaluation_key", "origin"}}
        new_scope = {k: v for k, v in params.items() if k not in {"evaluation_key", "origin"}}
        if (
            previous.config_id != config.id
            or previous.termination_reason not in {"budget", "stall"}
            or previous_scope != new_scope
        ):
            raise StateError("invalid_run_continuation")
        initial_progress = _bounded_json(previous.progress_json)
    row = TransactionRun(
        tenant_id=tenant_id,
        config_id=config.id,
        work_key=key,
        origin=request.origin,
        params_json=params,
        config_snapshot=ConfigOut.model_validate(config).model_dump(mode="json"),
        max_api_calls=config.max_api_calls,
        max_orders=config.max_orders,
        deadline_at=now + timedelta(seconds=config.deadline_seconds),
        initiated_by=actor.id if actor else None,
        progress_json=initial_progress,
    )
    db.add(row)
    await db.flush()
    await _audit(db, tenant_id, "run.create", row, actor)
    await _commit(db, tenant_id)
    return row


async def get_run(db, tenant_id, run_id, *, lock=False):
    return await _one(db, tenant_id, TransactionRun, run_id, lock=lock)


async def list_runs(db, tenant_id, *, config_id=None, runnable_only=False, limit=100):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionRun).where(TransactionRun.tenant_id == tenant_id)
    if config_id:
        query = query.where(TransactionRun.config_id == config_id)
    if runnable_only:
        query = query.where(TransactionRun.status.in_(("pending", "running")))
    return list(
        (await db.execute(query.order_by(TransactionRun.created_at.desc()).limit(min(200, max(1, limit))))).scalars()
    )


def _finish(row, reason, now):
    row.status, row.termination_reason, row.finished_at = "finished", reason, now
    row.lease_token = row.lease_until = None


def _lease(row, token, now):
    if row.status != "running" or token != row.lease_token or row.lease_until is None or now >= row.lease_until:
        raise StateError("run_lease_lost")


async def claim_run(db, tenant_id, run_id, *, now=None):
    now = _clock(now)
    row = await get_run(db, tenant_id, run_id, lock=True)
    if row.status == "finished":
        await _commit(db, tenant_id)
        return None
    if now >= row.deadline_at:
        _finish(row, "budget", now)
        await _commit(db, tenant_id)
        return None
    if row.status == "running" and row.lease_until and now < row.lease_until:
        await _commit(db, tenant_id)
        return None
    config = await get_config(db, tenant_id, row.config_id)
    if not config.enabled:
        _finish(row, "stall", now)
        await _commit(db, tenant_id)
        return None
    row.status, row.lease_token = "running", uuid.uuid4()
    row.lease_until = min(row.deadline_at, now + _LEASE)
    await _commit(db, tenant_id)
    return row.lease_token


async def reserve_budget(db, tenant_id, run_id, *, lease_token, api_calls=0, orders=0, now=None):
    if any(type(value) is not int or value < 0 for value in (api_calls, orders)) or api_calls + orders == 0:
        raise ValueError("Reserve positive integer spend before a call")
    now = _clock(now)
    row = await get_run(db, tenant_id, run_id, lock=True)
    if row.status == "finished":
        await _commit(db, tenant_id)
        return False
    if now >= row.deadline_at:
        _finish(row, "budget", now)
        await _commit(db, tenant_id)
        return False
    _lease(row, lease_token, now)
    if row.api_calls_used + api_calls > row.max_api_calls or row.orders_used + orders > row.max_orders:
        _finish(row, "budget", now)
        await _commit(db, tenant_id)
        return False
    row.api_calls_used += api_calls
    row.orders_used += orders
    row.lease_until = min(row.deadline_at, now + _LEASE)
    await _commit(db, tenant_id)
    return True


async def update_progress(db, tenant_id, run_id, request: ProgressUpdate, *, lease_token, now=None):
    now = _clock(now)
    row = await get_run(db, tenant_id, run_id, lock=True)
    _lease(row, lease_token, now)
    row.progress_json = request.progress_json
    row.lease_until = min(row.deadline_at, now + _LEASE)
    await _commit(db, tenant_id)
    return row


async def finish_run(db, tenant_id, run_id, reason: Termination, *, lease_token, now=None):
    if reason not in {"done", "budget", "stall", "error"}:
        raise ValueError("Invalid termination reason")
    now = _clock(now)
    row = await get_run(db, tenant_id, run_id, lock=True)
    if row.status == "finished":
        await _commit(db, tenant_id)
        return row
    # A provider call may consume the final instant of the run's deadline,
    # which also expires its lease. The same fenced owner may record only
    # budget termination in that case; expired owners cannot publish success,
    # or finish work reclaimed under a different token.
    if not (
        reason == "budget"
        and now >= row.deadline_at
        and row.status == "running"
        and lease_token is not None
        and lease_token == row.lease_token
    ):
        _lease(row, lease_token, now)
    _finish(row, reason, now)
    await _audit(db, tenant_id, "run.finish", row, payload={"reason": reason})
    await _commit(db, tenant_id)
    return row


async def get_proposal(db, tenant_id, proposal_id, *, lock=False):
    return await _one(db, tenant_id, TransactionProposal, proposal_id, lock=lock)


async def list_proposals(db, tenant_id, *, run_id=None, status=None, limit=100, offset=0):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionProposal).where(TransactionProposal.tenant_id == tenant_id)
    if run_id:
        query = query.where(TransactionProposal.run_id == run_id)
    if status:
        query = query.where(TransactionProposal.status == status)
    return list(
        (
            await db.execute(
                query.order_by(TransactionProposal.created_at.desc(), TransactionProposal.id)
                .offset(max(0, offset))
                .limit(min(200, max(1, limit)))
            )
        ).scalars()
    )


async def record_finding(db, tenant_id, run_id, order_reference, report_json, *, lease_token, now=None):
    now = _clock(now)
    request = FindingReport(order_reference=order_reference, report_json=report_json)
    run = await get_run(db, tenant_id, run_id, lock=True)
    _lease(run, lease_token, now)
    row = (
        await db.execute(
            select(TransactionFinding).where(
                TransactionFinding.tenant_id == tenant_id,
                TransactionFinding.run_id == run_id,
                TransactionFinding.order_reference == order_reference,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = TransactionFinding(tenant_id=tenant_id, run_id=run_id, **request.model_dump())
        db.add(row)
    else:
        row.report_json = request.report_json
    run.lease_until = min(run.deadline_at, now + _LEASE)
    await db.flush()
    await _commit(db, tenant_id)
    return row


async def list_findings(db, tenant_id, run_id, *, offset=0, limit=100):
    await get_run(db, tenant_id, run_id)
    query = (
        select(TransactionFinding)
        .where(
            TransactionFinding.tenant_id == tenant_id,
            TransactionFinding.run_id == run_id,
        )
        .order_by(TransactionFinding.order_reference)
        .offset(max(0, offset))
        .limit(min(100, max(1, limit)))
    )
    return list((await db.execute(query)).scalars())


async def propose(db, tenant_id, run_id, request: ProposalCreate, *, lease_token, now=None):
    now = _clock(now)
    if not timedelta(0) <= now - request.observed_at < _EVIDENCE_AGE:
        raise StateError("stale_evidence")
    run = await get_run(db, tenant_id, run_id, lock=True)
    _lease(run, lease_token, now)
    config = await get_config(db, tenant_id, run.config_id, lock=True)
    if not config.enabled:
        raise StateError("config_disabled")
    if (config.mapping_json or {}).get("action_mode", "detect_only") != "propose_actions":
        raise StateError("actions_disabled")
    run.lease_until = min(run.deadline_at, now + _LEASE)
    # The observation timestamp is freshness, not work identity. Before/after,
    # authoritative versions (fingerprint) and exact destination define the work.
    key = business_digest({"config": config.config_key, **request.model_dump(exclude={"observed_at", "evidence_json"})})
    existing = (
        await db.execute(
            select(TransactionProposal)
            .where(
                TransactionProposal.tenant_id == tenant_id,
                TransactionProposal.work_key == key,
            )
            .order_by(
                TransactionProposal.status.in_(("pending", "approved")).desc(),
                TransactionProposal.created_at.desc(),
                TransactionProposal.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        attempted = (
            await db.execute(
                select(TransactionOperation.id).where(
                    TransactionOperation.tenant_id == tenant_id,
                    TransactionOperation.work_key == key,
                )
            )
        ).scalar_one_or_none()
        if (
            existing.status == "rejected"
            or attempted is not None
            or (existing.status in {"pending", "approved"} and now < existing.valid_until)
        ):
            await _commit(db, tenant_id)
            return existing
        if existing.status in {"pending", "approved"}:
            existing.status = "superseded"
            await db.flush()
    row = TransactionProposal(
        tenant_id=tenant_id,
        config_id=config.id,
        run_id=run.id,
        work_key=key,
        netsuite_account_id=config.netsuite_account_id,
        subsidiary_id=config.subsidiary_id,
        record_type=config.record_type,
        valid_until=request.observed_at + _EVIDENCE_AGE,
        **request.model_dump(),
    )
    db.add(row)
    await db.flush()
    await _audit(db, tenant_id, "proposal.create", row)
    await _commit(db, tenant_id)
    return row


async def decide_proposal(db, tenant_id, proposal_id, request: ProposalDecision, *, actor, now=None):
    now = _clock(now)
    row = await get_proposal(db, tenant_id, proposal_id, lock=True)
    await _human(db, tenant_id, actor, "recon.run")
    if row.status != "pending":
        raise StateError("proposal_not_pending")
    if row.evidence_fingerprint != request.evidence_fingerprint:
        raise StateError("evidence_changed")
    if request.decision == "approve" and now >= row.valid_until:
        row.status = "superseded"
        await _audit(db, tenant_id, "proposal.expire", row, actor)
        await _commit(db, tenant_id)
        raise StateError("stale_evidence")
    row.status = "approved" if request.decision == "approve" else "rejected"
    row.decided_by, row.decided_at, row.decision_note = actor.id, now, request.note
    await _audit(db, tenant_id, request.decision, row, actor, {"evidence_fingerprint": row.evidence_fingerprint})
    await _commit(db, tenant_id)
    return row


async def claim_approved_operation(db, tenant_id, proposal_id, *, expected_evidence_fingerprint, now=None):
    """Claim the approved intent before fresh, separately budgeted provider reads.

    Matching an expected fingerprint here does not attest to fresh evidence.
    The executor must compare fresh evidence with it before dispatch; the
    provider adapter must enforce the final server-side conditional write.
    """
    now = _clock(now)
    await set_tenant_context(db, str(tenant_id))
    # Different proposals can concern the same external order. Serialize only
    # the short pre-call transaction; never hold this lock during network I/O.
    await db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    row = await get_proposal(db, tenant_id, proposal_id, lock=True)
    entity_key = business_digest(
        {
            "account": row.netsuite_account_id,
            "subsidiary": row.subsidiary_id,
            "record_type": row.record_type,
            "order_reference": row.order_reference,
        }
    )
    existing = (
        await db.execute(
            select(TransactionOperation.id).where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.work_key == row.work_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise StateError("operation_already_attempted")
    unresolved = (
        await db.execute(
            select(TransactionOperation.id).where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.entity_key == entity_key,
                TransactionOperation.status.in_(("executing", "unknown")),
            )
        )
    ).scalar_one_or_none()
    if unresolved is not None:
        raise StateError("operation_already_attempted")
    if row.status != "approved":
        raise StateError("proposal_not_approved")
    decider = (
        await db.execute(
            select(User)
            .where(
                User.id == row.decided_by,
                User.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    try:
        await _human(db, tenant_id, decider, "recon.run")
    except StateError:
        row.status = "superseded"
        await _audit(db, tenant_id, "proposal.invalidate", row, payload={"reason": "approval_actor_revoked"})
        await _commit(db, tenant_id)
        return None
    config = await get_config(db, tenant_id, row.config_id)
    if (
        not config.enabled
        or (config.mapping_json or {}).get("action_mode", "detect_only") != "propose_actions"
        or row.evidence_fingerprint != expected_evidence_fingerprint
        or now >= row.valid_until
    ):
        row.status = "superseded"
        await _audit(db, tenant_id, "proposal.invalidate", row)
        await _commit(db, tenant_id)
        return None
    operation = TransactionOperation(
        tenant_id=tenant_id,
        proposal_id=row.id,
        work_key=row.work_key,
        entity_key=entity_key,
        attempted_at=now,
        deadline_at=min(now + _OPERATION_TIME, row.valid_until),
        max_api_calls=_OPERATION_CALLS,
        api_calls_used=0,
        status="executing",
    )
    db.add(operation)
    await db.flush()
    intent = ClaimedOperation(
        operation_id=operation.id,
        proposal_id=row.id,
        work_key=row.work_key,
        config_id=row.config_id,
        action=row.action,
        currency=row.currency,
        netsuite_account_id=row.netsuite_account_id,
        subsidiary_id=row.subsidiary_id,
        record_type=row.record_type,
        target_record_id=row.target_record_id,
        before_json=row.before_json,
        after_json=row.after_json,
    )
    await _audit(db, tenant_id, "operation.attempt", operation)
    await _commit(db, tenant_id)
    return intent


async def complete_operation(db, tenant_id, operation_id, *, outcome, result_json, now=None):
    if outcome not in {"verified", "unknown", "failed"}:
        raise ValueError("Invalid operation outcome")
    evidence = _bounded_json(result_json)
    if _LEDGER_RESULT_KEYS.intersection(evidence):
        raise StateError("reserved_operation_result_key")
    row = await _one(db, tenant_id, TransactionOperation, operation_id, lock=True)
    if row.status not in {"executing", "unknown"}:
        raise StateError("operation_terminal")
    # An unknown attempt can become verified/failed only after caller-provided
    # read-only provider verification; this service never dispatches it again.
    if row.status == "unknown" and evidence.get("reconciled") is not True:
        raise StateError("reconciliation_evidence_required")
    row.status, row.completed_at = outcome, _clock(now)
    row.result_json = {
        **(row.result_json or {}),
        **evidence,
        "termination_reason": {"verified": "done", "unknown": "stall", "failed": "error"}[outcome],
    }
    await _audit(db, tenant_id, "operation.complete", row, payload={"outcome": outcome})
    await _commit(db, tenant_id)
    return row


async def reserve_operation_dispatch(
    db, tenant_id, claimed: ClaimedOperation, *, provider, payload_fingerprint, now=None
):
    """Consume one durable send permit. A crash after this commit permits only reads.

    An adapter calls this after its final fresh provider preflight and before
    its single mutation. No job retry or reconstructed claim can reserve again.
    Provider receipts never grant approval, reset the permit, or prove success.
    """
    now = _clock(now)
    if not isinstance(payload_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", payload_fingerprint):
        raise StateError("invalid_dispatch_fingerprint")
    await set_tenant_context(db, str(tenant_id))
    tenant = (
        await db.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise StateError("tenant_unavailable", 403)
    proposal = await get_proposal(db, tenant_id, claimed.proposal_id, lock=True)
    operation = await _one(db, tenant_id, TransactionOperation, claimed.operation_id, lock=True)
    expected = ClaimedOperation(
        operation_id=operation.id,
        proposal_id=proposal.id,
        work_key=proposal.work_key,
        config_id=proposal.config_id,
        action=proposal.action,
        currency=proposal.currency,
        netsuite_account_id=proposal.netsuite_account_id,
        subsidiary_id=proposal.subsidiary_id,
        record_type=proposal.record_type,
        target_record_id=proposal.target_record_id,
        before_json=proposal.before_json,
        after_json=proposal.after_json,
    )
    if claimed != expected or operation.proposal_id != proposal.id or operation.work_key != proposal.work_key:
        raise StateError("claimed_operation_mismatch")
    if (operation.result_json or {}).get("dispatch_reserved") is True:
        await _commit(db, tenant_id)
        return False
    if operation.status != "executing" or proposal.status != "approved":
        raise StateError("operation_not_executable")
    required_provider = {
        "correct_amounts": "netsuite",
        "sync_missing_order": "netsuite",
        "resolve_celigo_error": "celigo",
    }
    if required_provider.get(proposal.action) != provider:
        raise StateError("unsupported_dispatch_provider")
    if now >= proposal.valid_until:
        raise StateError("stale_evidence")
    config = await get_config(db, tenant_id, proposal.config_id)
    if not config.enabled or (config.mapping_json or {}).get("action_mode", "detect_only") != "propose_actions":
        raise StateError("actions_disabled")
    flags = await get_all_flags(db, tenant_id)
    if flags.get("celigo") is not True or flags.get("reconciliation") is not True:
        raise StateError("feature_disabled", 403)
    actor = (
        await db.execute(
            select(User)
            .where(User.tenant_id == tenant_id, User.id == proposal.decided_by)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    await _human(db, tenant_id, actor, "recon.run")
    if now >= operation.deadline_at or operation.api_calls_used >= operation.max_api_calls:
        await _exhaust_operation(db, tenant_id, operation, now, "operation_budget_exhausted")
        raise StateError("operation_budget_exhausted")
    operation.api_calls_used += 1
    operation.result_json = {
        **(operation.result_json or {}),
        "dispatch_reserved": True,
        "provider": provider,
        "payload_fingerprint": payload_fingerprint,
        "dispatch_reserved_at": now.isoformat(),
    }
    await _audit(
        db,
        tenant_id,
        "operation.dispatch",
        operation,
        payload={"provider": provider, "payload_fingerprint": payload_fingerprint},
    )
    await _commit(db, tenant_id)
    return True


async def _exhaust_operation(db, tenant_id, operation, now, code):
    # This lock is shared with the dispatch permit. An old worker cannot send
    # after recovery marks a pre-dispatch operation failed. A consumed permit
    # cannot be distinguished from a sent request, so it always stays unknown.
    operation.status = "unknown" if (operation.result_json or {}).get("dispatch_reserved") is True else "failed"
    operation.completed_at = now
    operation.result_json = {**(operation.result_json or {}), "termination_reason": "budget", "code": code}
    await _audit(db, tenant_id, "operation.exhaust", operation, payload={"outcome": operation.status, "code": code})
    await _commit(db, tenant_id)


async def reserve_operation_budget(db, tenant_id, operation_id, *, api_calls, now=None):
    """Commit the worst-case request cost before an execution-phase read.

    No refunds, deadline extensions or replay after a terminal/unknown state.
    A returned deadline must also bound the caller's transport timeout. Unknown
    outcomes use a separate read-only reconciliation job, never this permit.
    """
    if type(api_calls) is not int or not 1 <= api_calls <= _OPERATION_CALLS:
        raise ValueError("A bounded positive integer API-call cost is required")
    now = _clock(now)
    await set_tenant_context(db, str(tenant_id))
    tenant = (
        await db.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise StateError("tenant_unavailable", 403)
    operation = await _one(db, tenant_id, TransactionOperation, operation_id, lock=True)
    if operation.status != "executing":
        raise StateError("operation_not_executable")
    if now >= operation.deadline_at or operation.api_calls_used + api_calls > operation.max_api_calls:
        await _exhaust_operation(db, tenant_id, operation, now, "operation_budget_exhausted")
        return None
    flags = await get_all_flags(db, tenant_id)
    if flags.get("celigo") is not True or flags.get("reconciliation") is not True:
        raise StateError("feature_disabled", 403)
    operation.api_calls_used += api_calls
    permit = OperationReadPermit(
        deadline_at=operation.deadline_at, remaining_api_calls=operation.max_api_calls - operation.api_calls_used
    )
    await _audit(db, tenant_id, "operation.read_budget", operation, payload={"api_calls": api_calls})
    await _commit(db, tenant_id)
    return permit


async def recover_expired_operation(db, tenant_id, operation_id, *, now=None):
    """Fence a lost executor without ever obtaining another send permit."""
    now = _clock(now)
    operation = await _one(db, tenant_id, TransactionOperation, operation_id, lock=True)
    if operation.status != "executing" or now < operation.deadline_at:
        await _commit(db, tenant_id)
        return None
    sent = (operation.result_json or {}).get("dispatch_reserved") is True
    await _exhaust_operation(
        db, tenant_id, operation, now, "interrupted_after_dispatch" if sent else "interrupted_before_dispatch"
    )
    return operation
