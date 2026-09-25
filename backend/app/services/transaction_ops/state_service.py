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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from pydantic import ValidationError
from sqlalchemy import BigInteger, and_, cast, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.celigo import CeligoFlow, CeligoFlowStep
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.feature_flag import TenantFeatureFlag
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
_RUN_QUEUE_AGE = timedelta(days=1)
_EVIDENCE_AGE = timedelta(minutes=15)
_OPERATION_CALLS = 96
_OPERATION_TIME = timedelta(seconds=300)
_LEDGER_RESULT_KEYS = frozenset(
    {
        "dispatch_reserved",
        "provider",
        "payload_fingerprint",
        "dispatch_reserved_at",
        "termination_reason",
    }
)
# The write kernel's outcome taxonomy (docs/superpowers/specs/2026-09-15-write-kernel-design.md,
# section 3). The repair rule is a function of the status: a retry is allowed only from
# rejected_before_effect and only as a lineage row; unknown may only be reconciled by reads;
# committed_unverified may only be verified by reads; needs_review waits for a person.
OUTCOMES = frozenset({"rejected_before_effect", "committed_unverified", "unknown", "verified", "needs_review"})
TERMINAL = frozenset({"verified", "rejected_before_effect", "needs_review"})
SETTLED = frozenset({"unknown", "committed_unverified"})  # a permit was consumed; reads only from here
# The states the write kernel may still write to. ``unknown`` is settled but not open: only
# read-only reconciliation (recovery) may move it, never the attempt that produced it.
OPEN = frozenset({"executing", "committed_unverified"})
IN_FLIGHT = frozenset({"executing", *SETTLED})  # an attempt that blocks a new one on the same work or entity
TERMINATION = {
    "verified": "done",
    "unknown": "stall",
    "committed_unverified": "stall",
    "rejected_before_effect": "error",
    "needs_review": "blocked",
}
PROVIDERS = {"correct_amounts": "netsuite", "sync_missing_order": "netsuite", "resolve_celigo_error": "celigo"}
ADAPTERS = {"netsuite": "guard_restlet", "celigo": "celigo"}


class StateError(ValueError):
    def __init__(self, code: str, http_status: int = 409):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def lineage_work_key(base_work_key: str, retry_of_operation_id) -> str:
    """The work key of a retry: the business identity plus the attempt it retries, so the
    one-attempt-per-work rule admits it and the row still inherits the base key."""
    return business_digest({"base_work": base_work_key, "retry_of_operation": str(retry_of_operation_id)})


def permit_consumed(operation) -> bool:
    """Whether the one-use send permit was reserved on this row. A consumed permit cannot be
    told apart from a sent request, so every outcome after it is at least ``unknown``."""
    return (operation.result_json or {}).get("dispatch_reserved") is True


@dataclass(frozen=True)
class ApprovedIntent:
    """What an approval source hands the ledger to claim: the approval's identity, the
    business identity of the work (``work_key``), the collision scope (``entity_key``) and
    the provider/adapter that will carry it. The source has already checked the approval is
    valid; the ledger checks the work is new and the document free."""

    approval_kind: str
    approval_id: uuid.UUID
    approved_by: uuid.UUID
    surface: str
    provider: str
    adapter: str
    action: str
    work_key: str
    entity_key: str
    netsuite_account_id: str
    subsidiary_id: str
    record_type: str
    target_record_id: str | None
    evidence_digest: str
    valid_until: datetime
    currency: str | None = None
    config_id: uuid.UUID | None = None
    order_reference: str | None = None  # with config_id, the scope a read-only recovery runs under
    retry_of_operation_id: uuid.UUID | None = None


def _clock(now=None):
    value = now or datetime.now(timezone.utc)
    if value.utcoffset() is None:
        raise ValueError("An aware clock is required")
    return value


async def run_clock(db, now=None):
    """Use the same clock as PostgreSQL's immutable run-budget guard.

    Explicit clocks remain available for deterministic internal callers/tests.
    Host clock drift must not extend a budget or reject a valid first claim.
    """
    if now is None:
        now = await db.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime):
            raise StateError("run_clock_unavailable")
    return _clock(now)


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
    # Keep exact evidence and changes in their immutable database records. Every
    # event carries durable links; request correlation alone does not span jobs.
    links = {}
    for key in ("config_id", "run_id", "proposal_id"):
        value = getattr(row, key, None)
        if value is not None:
            links[key] = str(value)
    if isinstance(row, TransactionRun):
        links["run_id"] = str(row.id)
    elif isinstance(row, TransactionProposal):
        links.update(proposal_id=str(row.id), evidence_fingerprint=row.evidence_fingerprint)
        if row.decided_by is not None:
            links.update(decided_by=str(row.decided_by), decided_at=row.decided_at.isoformat())
    elif isinstance(row, TransactionOperation):
        links["operation_id"] = str(row.id)
    await audit_service.log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action=f"transaction_ops.{action}",
        actor_id=actor.id if actor else None,
        actor_type="user" if actor else "system",
        resource_type=row.__tablename__,
        resource_id=str(row.id),
        payload={**(payload or {}), **links},
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
    replica = TransactionMapping.model_validate(request.mapping_json).metabase_replica
    if replica:
        from app.services.transaction_ops.metabase_reader import ReplicaReadError, _connector

        try:
            await _connector(db, tenant_id, replica)
        except ReplicaReadError:
            raise StateError("replica_unavailable", 422) from None
    # These are local ownership/lifecycle checks. The root runner independently
    # validates live Framework/Celigo/provider configuration before any read/write.
    if request.source_connection_id is not None:
        source = (
            await db.execute(
                select(Connection.id).where(
                    Connection.id == request.source_connection_id,
                    Connection.tenant_id == tenant_id,
                    Connection.provider == "solidus",
                    Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
                    Connection.metadata_json["api_profile"].as_string() == "framework_sync",
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise StateError("source_unavailable", 422)
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


def current_config_clause():
    successor = aliased(TransactionConfig)
    return (
        ~select(successor.id)
        .where(
            successor.tenant_id == TransactionConfig.tenant_id,
            successor.supersedes_config_id == TransactionConfig.id,
        )
        .exists()
    )


async def list_configs(db, tenant_id, *, scheduled_only=False):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionConfig).where(TransactionConfig.tenant_id == tenant_id, current_config_clause())
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
    if request.enabled and await db.scalar(
        select(TransactionConfig.id).where(
            TransactionConfig.tenant_id == tenant_id, TransactionConfig.supersedes_config_id == row.id
        )
    ):
        raise StateError("config_superseded", 409)
    if request.schedule_enabled and not request.enabled:
        raise StateError("disabled_config_cannot_schedule", 422)
    row.enabled, row.schedule_enabled = request.enabled, request.schedule_enabled
    await _audit(db, tenant_id, "config.control", row, actor, request.model_dump())
    await _commit(db, tenant_id)
    return row


def _config_snapshot(config):
    from app.services.transaction_ops.evidence_contract import VERSION

    return {
        **ConfigOut.model_validate(config).model_dump(mode="json"),
        "evidence_contract_version": VERSION,
        "destination_discovery_version": 2
        if config.mapping_json.get("metabase_replica") and config.mapping_json.get("reconciliation_policy") is not None
        else 1,
    }


async def create_run(
    db,
    tenant_id,
    config_id,
    request: RunCreate,
    *,
    actor=None,
    now=None,
    resume_from_run_id=None,
    automatic_continuation=False,
    human_retry=False,
):
    now = await run_clock(db, now)
    if human_retry and (
        request.origin != "manual" or automatic_continuation or not request.review or resume_from_run_id is None
    ):
        raise StateError("invalid_run_continuation")
    config = await get_config(db, tenant_id, config_id, lock=True)
    if not config.enabled:
        raise StateError("config_disabled")
    if request.origin == "schedule":
        if not config.schedule_enabled:
            raise StateError("schedule_disabled")
    else:
        await _human(db, tenant_id, actor, "recon.run")
    if request.window_basis == "completed_at" and not (config.mapping_json or {}).get("metabase_replica"):
        raise StateError("period_reader_unavailable", 422)
    # Preserve idempotency for requests made before calendar cohorts were added.
    excluded = {"window_basis"} if request.window_basis == "updated_at" and request.review is None else set()
    if request.review is None:
        excluded.add("review")
    elif request.review.end > now:
        raise StateError("review_period_not_closed", 422)
    params = request.model_dump(mode="json", exclude=excluded)
    key = business_digest({"config": config.config_key, "params": request.model_dump(exclude=excluded)})
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
            or previous.status != "finished"
            or previous.termination_reason not in ({"budget", "stall", "error"} if human_retry else {"budget", "stall"})
            or previous_scope != new_scope
        ):
            raise StateError("invalid_run_continuation")
        initial_progress = _bounded_json(previous.progress_json)
        if request.origin == "schedule":
            initial_progress["evidence_root_id"] = str(
                uuid.UUID(
                    initial_progress.get("evidence_root_id")
                    or initial_progress.get("continuation_root_id")
                    or str(previous.id)
                )
            )
            if automatic_continuation and not initial_progress.get("schedule_cycle_key"):
                from app.services.transaction_ops.scheduler import _cycle_key

                initial_progress["schedule_cycle_key"] = _cycle_key(config, previous)
            if not automatic_continuation:
                # A new scheduled cycle must earn subsequent parts through new
                # work; cumulative evidence is not new productivity.
                initial_progress["schedule_cycle_key"] = request.evaluation_key
        for field in list(initial_progress):
            if field.startswith("continuation_"):
                initial_progress.pop(field)
        if human_retry:
            attempt = initial_progress.get("review_attempt", 0)
            if type(attempt) is not int or not 0 <= attempt < 512:
                raise StateError("invalid_run_continuation")
            initial_progress.update(
                review_attempt=attempt + 1,
                continuation_of=str(previous.id),
                # Cumulative evidence survives, but it is not new productivity
                # that can authorize an unattended continuation of this retry.
                continuation_baseline={
                    key: initial_progress.get(key, 0)
                    for key in (
                        "processed",
                        "scan_count",
                        "refund_scan_count",
                        "outside_scope",
                        "destination_scan_count",
                        "dependency_step_count",
                    )
                },
            )
        if automatic_continuation:
            from app.services.transaction_ops.continuation import next_metadata

            metadata = next_metadata(previous, now)
            if (
                previous.termination_reason != "budget"
                or request.origin != previous.origin
                or request.evaluation_key
                != f"continue:{metadata['continuation_root_id']}:{metadata['continuation_part']}"
            ):
                raise StateError("invalid_run_continuation")
            initial_progress.update(metadata)
    elif automatic_continuation:
        raise StateError("invalid_run_continuation")
    if request.origin == "schedule":
        initial_progress.setdefault("schedule_cycle_key", request.evaluation_key)
        if resume_from_run_id is not None and not automatic_continuation:
            initial_progress["continuation_baseline"] = {
                field: initial_progress.get(field, 0)
                for field in (
                    "processed",
                    "scan_count",
                    "refund_scan_count",
                    "outside_scope",
                    "destination_scan_count",
                    "dependency_step_count",
                )
            }
    row = TransactionRun(
        tenant_id=tenant_id,
        config_id=config.id,
        work_key=key,
        origin=request.origin,
        params_json=params,
        config_snapshot=_config_snapshot(config),
        max_api_calls=config.max_api_calls,
        max_orders=config.max_orders,
        deadline_at=now + timedelta(seconds=config.deadline_seconds),
        initiated_by=actor.id if actor else None,
        progress_json=initial_progress,
    )
    if human_retry:
        row.created_at = now
    db.add(row)
    await db.flush()
    await _audit(db, tenant_id, "run.create", row, actor)
    await _commit(db, tenant_id)
    return row


async def get_run(db, tenant_id, run_id, *, lock=False):
    return await _one(db, tenant_id, TransactionRun, run_id, lock=lock)


async def list_runs(db, tenant_id, *, config_id=None, runnable_only=False, period_reviews_only=False, limit=100):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionRun).where(TransactionRun.tenant_id == tenant_id)
    if config_id:
        query = query.where(TransactionRun.config_id == config_id)
    if runnable_only:
        query = query.where(TransactionRun.status.in_(("pending", "running")))
    if period_reviews_only:
        # Select one representative per saved review BEFORE limiting. Daily
        # scans and continuations must not push older review cohorts off-screen.
        review_id = TransactionRun.params_json["review"]["id"].astext
        latest = (
            query.where(review_id.is_not(None))
            .distinct(TransactionRun.config_id, review_id)
            .order_by(TransactionRun.config_id, review_id, TransactionRun.created_at.desc(), TransactionRun.id.desc())
            .subquery()
        )
        model = aliased(TransactionRun, latest)
        return list(
            (
                await db.scalars(
                    select(model).order_by(model.created_at.desc(), model.id.desc()).limit(min(200, max(1, limit)))
                )
            )
        )
    return list(
        (await db.execute(query.order_by(TransactionRun.created_at.desc()).limit(min(200, max(1, limit))))).scalars()
    )


def _finish(row, reason, now):
    # An unsettled hold is assumed spent: a crash or a failed settle cannot under-count.
    row.api_calls_used += row.api_calls_held or 0
    row.api_calls_held = 0
    row.status, row.termination_reason, row.finished_at = "finished", reason, now
    row.lease_token = row.lease_until = None


async def _finish_audited(db, tenant_id, row, reason, now):
    from app.services.transaction_ops.settlement import is_settlement, record_outcome

    if is_settlement(row):
        await record_outcome(db, tenant_id, row, reason, now=now)
    _finish(row, reason, now)
    await _audit(db, tenant_id, "run.finish", row, payload={"reason": reason})


def _lease(row, token, now):
    if row.status != "running" or token != row.lease_token or row.lease_until is None or now >= row.lease_until:
        raise StateError("run_lease_lost")


def _first_claim_deadline(row, now):
    """One execution budget after queueing, bounded by the original work's age."""
    seconds = (row.config_snapshot or {}).get("deadline_seconds")
    if type(seconds) is not int or not 30 <= seconds <= 3600:
        return row.deadline_at  # Legacy/malformed snapshots cannot acquire extra time.
    duration = timedelta(seconds=seconds)
    queued_at = row.deadline_at - duration
    hard_deadline = queued_at + _RUN_QUEUE_AGE
    started = (row.progress_json or {}).get("continuation_started_at")
    if started is not None:
        try:
            started = datetime.fromisoformat(started)
            if started.utcoffset() is None or started > now:
                return None
        except (TypeError, ValueError):
            return None
        hard_deadline = min(hard_deadline, started + _RUN_QUEUE_AGE)
    if now < queued_at or now >= hard_deadline:
        return None
    return min(now + duration, hard_deadline)


async def claim_run(db, tenant_id, run_id, *, now=None):
    from app.services.transaction_ops.settlement import is_settlement

    row = await get_run(db, tenant_id, run_id, lock=True)
    now = await run_clock(db, now)
    if row.status == "finished":
        await _commit(db, tenant_id)
        return None
    deadline = row.deadline_at
    if (
        row.status == "pending"
        and (row.origin in {"manual", "chat", "schedule"} or is_settlement(row))
        and row.lease_token is None
        and row.api_calls_used == row.orders_used == (row.api_calls_held or 0) == 0
    ):
        deadline = _first_claim_deadline(row, now)
    if deadline is None or now >= deadline:
        await _finish_audited(db, tenant_id, row, "budget", now)
        await _commit(db, tenant_id)
        return None
    if row.status == "running" and row.lease_until and now < row.lease_until:
        await _commit(db, tenant_id)
        return None
    config = await get_config(db, tenant_id, row.config_id)
    if not config.enabled or (row.origin == "schedule" and not config.schedule_enabled):
        await _finish_audited(db, tenant_id, row, "stall", now)
        await _commit(db, tenant_id)
        return None
    row.deadline_at = deadline
    row.status, row.lease_token = "running", uuid.uuid4()
    row.lease_until = min(row.deadline_at, now + _LEASE)
    await _commit(db, tenant_id)
    return row.lease_token


async def _update_owned_run(db, tenant_id, run_id, lease_token, now, *, values, conditions=()):
    """One round trip for a fenced update; rejected transitions retain the slow path.

    PostgreSQL locks the matching row and rechecks predicates after concurrent
    updates. Returning the ORM row refreshes any existing identity, just as
    get_run(populate_existing=True) does. The caller must commit before using
    the result to perform external work.
    """
    await set_tenant_context(db, str(tenant_id))
    return await db.scalar(
        update(TransactionRun)
        .where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.id == run_id,
            TransactionRun.status == "running",
            TransactionRun.lease_token == lease_token,
            TransactionRun.lease_until > now,
            *conditions,
        )
        .values(**values, lease_until=func.least(TransactionRun.deadline_at, now + _LEASE))
        .returning(TransactionRun)
        .execution_options(synchronize_session=False, populate_existing=True)
    )


async def reserve_budget(db, tenant_id, run_id, *, lease_token, api_calls=0, orders=0, hold=False, now=None):
    """Pay for calls before making them; the run ends on ``budget`` if they do not fit.

    With ``hold`` the calls are held rather than spent, for a read that will report what
    it actually sent through ``settle_budget``. Either way they count against the ceiling
    from this moment.
    """
    if any(type(value) is not int or value < 0 for value in (api_calls, orders)) or api_calls + orders == 0:
        raise ValueError("Reserve positive integer spend before a call")
    now = _clock(now)
    held = func.coalesce(TransactionRun.api_calls_held, 0)
    values = {"orders_used": TransactionRun.orders_used + orders}
    if hold:
        values["api_calls_held"] = held + api_calls
    else:
        values["api_calls_used"] = TransactionRun.api_calls_used + api_calls
    row = None
    if max(api_calls, orders) <= 2**31 - 1:
        row = await _update_owned_run(
            db,
            tenant_id,
            run_id,
            lease_token,
            now,
            values=values,
            conditions=(
                TransactionRun.deadline_at > now,
                cast(TransactionRun.api_calls_used, BigInteger) + held + api_calls <= TransactionRun.max_api_calls,
                cast(TransactionRun.orders_used, BigInteger) + orders <= TransactionRun.max_orders,
            ),
        )
    if row is not None:
        await _commit(db, tenant_id)
        return True
    # Preserve exact failure precedence and audited budget termination, including
    # the historical deadline-before-lease check. No rejected UPDATE spent calls.
    row = await get_run(db, tenant_id, run_id, lock=True)
    if row.status == "finished":
        await _commit(db, tenant_id)
        return False
    if now >= row.deadline_at:
        await _finish_audited(db, tenant_id, row, "budget", now)
        await _commit(db, tenant_id)
        return False
    _lease(row, lease_token, now)
    held = row.api_calls_held or 0
    if row.api_calls_used + held + api_calls > row.max_api_calls or row.orders_used + orders > row.max_orders:
        await _finish_audited(db, tenant_id, row, "budget", now)
        await _commit(db, tenant_id)
        return False
    if hold:
        # Settled at what the read sent (settle_budget); unsettled, it is charged in full
        # when the run finishes, which is the worst-case charge a plain reservation makes.
        row.api_calls_held = held + api_calls
    else:
        row.api_calls_used += api_calls
    row.orders_used += orders
    row.lease_until = min(row.deadline_at, now + _LEASE)
    await _commit(db, tenant_id)
    return True


async def settle_budget(db, tenant_id, run_id, *, lease_token, release, spent, now=None):
    """Settle a read's hold: charge what it sent, drop the rest of what it reserved.

    Reserving the worst case before each read is what keeps a run under its ceiling, and
    that stays true: a settle moves ``spent`` from held to used and frees only the part the
    read demonstrably did not send, so used + held never grows and used never falls. Held
    to the same lease as the reservation, so only the worker that reserved can settle. A
    finished run is left alone: finishing already charged its holds in full.
    """
    if any(type(value) is not int for value in (release, spent)) or not 0 <= spent <= release:
        raise ValueError("Settle whole calls, spending no more than was released")
    if release == 0:
        return False
    now = _clock(now)
    row = None
    if release <= 2**31 - 1:
        row = await _update_owned_run(
            db,
            tenant_id,
            run_id,
            lease_token,
            now,
            values={
                "api_calls_held": TransactionRun.api_calls_held - release,
                "api_calls_used": TransactionRun.api_calls_used + spent,
            },
            conditions=(func.coalesce(TransactionRun.api_calls_held, 0) >= release,),
        )
    if row is not None:
        await _commit(db, tenant_id)
        return True
    row = await get_run(db, tenant_id, run_id, lock=True)
    if row.status == "finished":
        await _commit(db, tenant_id)
        return False
    _lease(row, lease_token, now)
    if release > (row.api_calls_held or 0):
        raise StateError("run_hold_exceeded")
    row.api_calls_held -= release
    row.api_calls_used += spent
    row.lease_until = min(row.deadline_at, now + _LEASE)
    await _commit(db, tenant_id)
    return True


async def update_progress(db, tenant_id, run_id, request: ProgressUpdate, *, lease_token, now=None):
    now = _clock(now)
    row = await _update_owned_run(
        db, tenant_id, run_id, lease_token, now, values={"progress_json": request.progress_json}
    )
    if row is not None:
        await _commit(db, tenant_id)
        return row
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
    await _finish_audited(db, tenant_id, row, reason, now)
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


async def record_finding(
    db, tenant_id, run_id, order_reference, report_json, *, lease_token, now=None, final=True, checkpoint=None
):
    now = _clock(now)
    if checkpoint is not None and (not final or not isinstance(checkpoint, ProgressUpdate)):
        raise ValueError("Only a final finding may commit a validated progress checkpoint")
    run = await get_run(db, tenant_id, run_id, lock=True)
    _lease(run, lease_token, now)
    row = await _record_finding(db, tenant_id, run, order_reference, report_json, now=now, final=final)
    if checkpoint is not None:
        run.progress_json = checkpoint.progress_json
    await _commit(db, tenant_id)
    return row


async def record_finding_batch(db, tenant_id, run_id, reports, *, lease_token, checkpoint, now=None):
    """Publish a bounded, contiguous prefix and its case/audit history atomically.

    No provider work may occur inside this transaction. Review findings use
    set-based writes; other lifecycle rules retain their single writer.
    """
    if not isinstance(reports, list) or not 1 <= len(reports) <= 10 or not isinstance(checkpoint, ProgressUpdate):
        raise ValueError("invalid_finding_batch")
    references = [report["order_reference"] for report in reports]
    if len(set(references)) != len(references):
        raise ValueError("duplicate_finding_batch_reference")
    now = _clock(now)
    run = await get_run(db, tenant_id, run_id, lock=True)
    _lease(run, lease_token, now)
    config = await get_config(db, tenant_id, run.config_id)
    active = await db.scalar(
        select(
            exists().where(
                Tenant.id == tenant_id,
                Tenant.is_active.is_(True),
                *(
                    exists().where(
                        TenantFeatureFlag.tenant_id == tenant_id,
                        TenantFeatureFlag.flag_key == key,
                        TenantFeatureFlag.enabled.is_(True),
                    )
                    for key in ("celigo", "reconciliation")
                ),
            )
        )
    )
    if not config.enabled or not active:
        raise StateError("batch_disabled")
    if run.origin == "recovery":
        raise ValueError("recovery_cannot_batch_findings")
    pending = (run.progress_json or {}).get("pending_refs", [])
    if (
        pending[: len(references)] != references
        or checkpoint.progress_json.get("pending_refs") != pending[len(references) :]
        or checkpoint.progress_json.get("processed") != (run.progress_json or {}).get("processed", 0) + len(references)
    ):
        raise ValueError("noncontiguous_finding_batch")
    from app.services.transaction_ops import finding_batch

    if finding_batch.eligible(reports, now):
        rows = await finding_batch.persist(db, tenant_id, run, reports, now=now)
        run.lease_until = min(run.deadline_at, now + _LEASE)
    else:
        # Cleared/excluded findings retain their existing operation checks.
        rows = []
        for report in sorted(reports, key=lambda item: item["order_reference"]):
            rows.append(
                await _record_finding(db, tenant_id, run, report["order_reference"], report, now=now, final=True)
            )
    run.progress_json = checkpoint.progress_json
    await _commit(db, tenant_id)
    return rows


async def _record_finding(db, tenant_id, run, order_reference, report_json, *, now, final):
    request = FindingReport(order_reference=order_reference, report_json=report_json)
    if request.report_json.get("order_reference", order_reference) != order_reference:
        raise StateError("finding_order_mismatch", 422)
    request = request.model_copy(
        update={"report_json": {k: v for k, v in request.report_json.items() if k not in {"case_id", "_observation"}}}
    )
    if run.origin == "recovery" and run.params_json.get("approval_message_id"):
        from app.services.transaction_ops.accounting_recheck import bound_report

        # Every write is bound to the approval; only the final one may spend the
        # subledger read budget on the expensive recheck.
        request = request.model_copy(
            update={
                "report_json": await bound_report(
                    db, tenant_id, run, request.report_json, now=now, subledger_recheck=final
                )
            }
        )
    from app.services.transaction_ops.case_service import observation_time

    if isinstance(request.report_json.get("balance"), dict) or isinstance(request.report_json.get("comparison"), dict):
        request = request.model_copy(
            update={
                "report_json": {
                    **request.report_json,
                    "_observation": {
                        "final": final,
                        "observed_at": observation_time(request.report_json, now).isoformat(),
                    },
                }
            }
        )
    # The run lock serializes writers. Upsert returns the existing finding ID
    # and refreshes its ORM state in one round trip, including partial -> final.
    statement = insert(TransactionFinding).values(tenant_id=tenant_id, run_id=run.id, **request.model_dump())
    row = await db.scalar(
        statement.on_conflict_do_update(
            index_elements=["tenant_id", "run_id", "order_reference"],
            set_={"report_json": statement.excluded.report_json, "updated_at": func.now()},
        )
        .returning(TransactionFinding)
        .execution_options(populate_existing=True)
    )
    run.lease_until = min(run.deadline_at, now + _LEASE)
    from app.services.transaction_ops.dependency_index import record_dependencies

    await record_dependencies(db, tenant_id, run, row)
    from app.services.transaction_ops.case_service import observe_finding

    case = await observe_finding(db, tenant_id, run, row, now=now) if final else None
    if case is not None:
        row.report_json = {**row.report_json, "case_id": str(case.id)}
        await db.flush()
    from app.services.transaction_ops.source_eligibility import excluded_report

    if final and excluded_report(row.report_json):
        await _audit(
            db,
            tenant_id,
            "source.excluded",
            run,
            payload={
                "finding_id": str(row.id),
                "order_reference": order_reference,
                **row.report_json["source_eligibility"],
            },
        )
    return row


async def unseen_references(db, tenant_id, run_id, references, *, since=None):
    run = await get_run(db, tenant_id, run_id)
    root = uuid.UUID(
        (run.progress_json or {}).get("evidence_root_id")
        or (run.progress_json or {}).get("continuation_root_id")
        or str(run.id)
    )
    # A dependency event can invalidate an order already seen in this cycle.
    # Require finalized evidence whose oldest financial read is after the event
    # window. JSON timestamps are cast only after the trusted final marker; old
    # diagnostics have no marker and therefore cannot suppress a fresh read.
    from sqlalchemy import DateTime, cast

    evidence_filter = []
    if since is not None:
        since = datetime.fromisoformat(since) if isinstance(since, str) else since
        if since.utcoffset() is None:
            raise StateError("invalid_observation_floor", 422)
        evidence_filter = [
            TransactionFinding.report_json["_observation"]["final"].astext == "true",
            cast(TransactionFinding.report_json["_observation"]["observed_at"].astext, DateTime(timezone=True))
            >= since,
        ]
    seen = set(
        (
            await db.scalars(
                select(TransactionFinding.order_reference)
                .join(
                    TransactionRun,
                    (TransactionRun.id == TransactionFinding.run_id) & (TransactionRun.tenant_id == tenant_id),
                )
                .where(
                    *evidence_filter,
                    TransactionFinding.tenant_id == tenant_id,
                    TransactionFinding.order_reference.in_(references),
                    TransactionRun.config_id == run.config_id,
                    (TransactionRun.id == root)
                    | (TransactionRun.progress_json["continuation_root_id"].astext == str(root))
                    | (TransactionRun.progress_json["evidence_root_id"].astext == str(root)),
                )
            )
        ).all()
    )
    return [reference for reference in references if reference not in seen]


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
    if run.origin == "recovery":
        raise StateError("read_only_verification_run")
    config = await get_config(db, tenant_id, run.config_id, lock=True)
    if not config.enabled:
        raise StateError("config_disabled")
    if (config.mapping_json or {}).get("action_mode", "detect_only") != "propose_actions":
        raise StateError("actions_disabled")
    run.lease_until = min(run.deadline_at, now + _LEASE)
    # The observation timestamp is freshness, not work identity. Before/after,
    # authoritative versions (fingerprint) and exact destination define the work.
    key = business_digest({"config": config.config_key, **request.model_dump(exclude={"observed_at", "evidence_json"})})
    base_key, previous_attempt = key, None
    # At most two separately approved attempts for identical economic work.
    # Each generation retains its own immutable decision and operation ledger.
    for attempt_number in (1, 2):
        existing = (
            await db.execute(
                select(TransactionProposal)
                .where(TransactionProposal.tenant_id == tenant_id, TransactionProposal.work_key == key)
                .order_by(
                    TransactionProposal.status.in_(("pending", "approved")).desc(),
                    TransactionProposal.created_at.desc(),
                    TransactionProposal.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            break
        attempted = (
            await db.execute(
                select(TransactionOperation).where(
                    TransactionOperation.tenant_id == tenant_id, TransactionOperation.work_key == key
                )
            )
        ).scalar_one_or_none()
        if existing.status == "rejected":
            await _commit(db, tenant_id)
            return existing
        if attempted is not None:
            known_no_write = attempted.status in ("failed", "rejected_before_effect") and (
                not permit_consumed(attempted)
                or (attempted.result_json or {}).get("code") == "provider_rejected_without_save"
            )
            if not known_no_write or attempt_number == 2:
                await _commit(db, tenant_id)
                return existing
            previous_attempt = attempted.id
            key = business_digest({"base_work": base_key, "retry_of_operation": attempted.id})
            continue
        if existing.status in {"pending", "approved"} and now < existing.valid_until:
            await _commit(db, tenant_id)
            return existing
        if existing.status in {"pending", "approved"}:
            existing.status = "superseded"
            await db.flush()
        break
    values = request.model_dump()
    if previous_attempt is not None:
        values["evidence_json"] = _bounded_json(
            {**values["evidence_json"], "retry": {"previous_operation_id": str(previous_attempt), "attempt": 2}}
        )
    row = TransactionProposal(
        tenant_id=tenant_id,
        config_id=config.id,
        run_id=run.id,
        work_key=key,
        netsuite_account_id=config.netsuite_account_id,
        subsidiary_id=config.subsidiary_id,
        record_type=config.record_type,
        valid_until=request.observed_at + _EVIDENCE_AGE,
        **values,
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
            "account": row.netsuite_account_id.replace("_", "-").lower(),
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
    related = aliased(TransactionProposal)
    unresolved = (
        await db.execute(
            select(TransactionOperation.id)
            .join(related, and_(related.id == TransactionOperation.proposal_id, related.tenant_id == tenant_id))
            .where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.status.in_(IN_FLIGHT),
                func.lower(func.replace(related.netsuite_account_id, "_", "-"))
                == row.netsuite_account_id.replace("_", "-").lower(),
                related.subsidiary_id == row.subsidiary_id,
                related.record_type == row.record_type,
                related.order_reference == row.order_reference,
            )
            .limit(1)
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
    # Lineage: a retry proposal names the attempt it corrects; the new row keeps that
    # attempt's base work key so the business identity is never changed to pass the
    # duplicate check.
    retry = (row.evidence_json or {}).get("retry") or {}
    previous = None
    if retry.get("previous_operation_id"):
        previous = (
            await db.execute(
                select(TransactionOperation).where(
                    TransactionOperation.tenant_id == tenant_id,
                    TransactionOperation.id == uuid.UUID(str(retry["previous_operation_id"])),
                )
            )
        ).scalar_one_or_none()
    provider = PROVIDERS.get(row.action)
    operation = TransactionOperation(
        tenant_id=tenant_id,
        proposal_id=row.id,
        approval_kind="transaction_proposal",
        approval_id=row.id,
        surface="scheduled",
        provider=provider,
        adapter=ADAPTERS.get(provider),
        work_key=row.work_key,
        entity_key=entity_key,
        base_work_key=previous.base_work_key if previous is not None else row.work_key,
        retry_of_operation_id=previous.id if previous is not None else None,
        attempted_at=now,
        deadline_at=min(now + _OPERATION_TIME, row.valid_until),
        max_api_calls=_OPERATION_CALLS,
        api_calls_used=0,
        status="executing",
        # The scope a read-only recovery runs under, recorded at the claim like every
        # other source's (create_operation_recovery reads it from the row).
        result_json={"recovery_scope": {"config_id": str(row.config_id), "order_reference": row.order_reference}},
    )
    db.add(operation)
    await db.flush()
    intent = _claimed_from_proposal(operation.id, row)
    await _audit(db, tenant_id, "operation.attempt", operation)
    await _commit(db, tenant_id)
    return intent


async def claim_intent(db, tenant_id, intent: ApprovedIntent, *, now=None) -> ClaimedOperation:
    """Claim approved work from any approval source before its fresh, budgeted reads.

    The source (chat_confirmation.claim, or claim_approved_operation for a proposal) has
    already established the approval is valid. This is the generic half: the tenant lock,
    one attempt per approval, one attempt per piece of work (``work_key``), one in-flight
    attempt per document (``entity_key``; the partial unique index is the backstop), the
    lineage of a retry, the executing row, its audit and the commit.
    """
    now = _clock(now)
    await set_tenant_context(db, str(tenant_id))
    await db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    claimed = (
        await db.execute(
            select(TransactionOperation.id).where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.approval_kind == intent.approval_kind,
                TransactionOperation.approval_id == intent.approval_id,
            )
        )
    ).scalar_one_or_none()
    if claimed is not None:
        raise StateError("approval_already_claimed")
    attempted = (
        await db.execute(
            select(TransactionOperation.id).where(
                TransactionOperation.tenant_id == tenant_id, TransactionOperation.work_key == intent.work_key
            )
        )
    ).scalar_one_or_none()
    if attempted is not None:
        raise StateError("operation_already_attempted")
    in_flight = (
        await db.execute(
            select(TransactionOperation.id).where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.entity_key == intent.entity_key,
                TransactionOperation.status.in_(IN_FLIGHT),
            )
        )
    ).scalar_one_or_none()
    if in_flight is not None:
        raise StateError("entity_in_flight")
    previous = None
    if intent.retry_of_operation_id is not None:
        previous = await _one(db, tenant_id, TransactionOperation, intent.retry_of_operation_id)
        if previous.status != "rejected_before_effect":
            raise StateError("retry_requires_rejected_before_effect")
    operation = TransactionOperation(
        tenant_id=tenant_id,
        proposal_id=None,
        approval_kind=intent.approval_kind,
        approval_id=intent.approval_id,
        surface=intent.surface,
        provider=intent.provider,
        adapter=intent.adapter,
        work_key=intent.work_key,
        entity_key=intent.entity_key,
        base_work_key=previous.base_work_key if previous is not None else intent.work_key,
        retry_of_operation_id=previous.id if previous is not None else None,
        attempted_at=now,
        deadline_at=min(now + _OPERATION_TIME, intent.valid_until),
        max_api_calls=_OPERATION_CALLS,
        api_calls_used=0,
        status="executing",
        result_json={
            "evidence_digest": intent.evidence_digest,
            "approved_by": str(intent.approved_by),
            # Recorded at the claim so a recovery pass reads under the scope the approval had,
            # never one a later caller supplies.
            "recovery_scope": {
                "config_id": str(intent.config_id) if intent.config_id else None,
                "order_reference": intent.order_reference,
            },
        },
    )
    db.add(operation)
    await db.flush()
    claimed = ClaimedOperation(
        operation_id=operation.id,
        proposal_id=None,
        approval_kind=intent.approval_kind,
        approval_id=intent.approval_id,
        work_key=intent.work_key,
        config_id=intent.config_id,
        action=intent.action,
        currency=intent.currency,
        netsuite_account_id=intent.netsuite_account_id,
        subsidiary_id=intent.subsidiary_id,
        record_type=intent.record_type,
        target_record_id=intent.target_record_id,
        before_json={},
        after_json={},
    )
    await _audit(
        db,
        tenant_id,
        "operation.attempt",
        operation,
        payload={
            "approval_kind": intent.approval_kind,
            "approval_id": str(intent.approval_id),
            "surface": intent.surface,
        },
    )
    await _commit(db, tenant_id)
    return claimed


async def operation_for_approval(db, tenant_id, approval_kind, approval_id):
    """The ledger row an approval claimed, or None."""
    await set_tenant_context(db, str(tenant_id))
    return (
        await db.execute(
            select(TransactionOperation)
            .where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.approval_kind == approval_kind,
                TransactionOperation.approval_id == approval_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def latest_operation_for_base(db, tenant_id, base_work_key):
    """The most recent attempt on a business identity across its retry lineage, or None."""
    await set_tenant_context(db, str(tenant_id))
    return (
        await db.execute(
            select(TransactionOperation)
            .where(TransactionOperation.tenant_id == tenant_id, TransactionOperation.base_work_key == base_work_key)
            .order_by(TransactionOperation.attempted_at.desc(), TransactionOperation.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def operation_for_work(db, tenant_id, work_key):
    """The ledger row for a piece of work, or None; the duplicate check's own lookup."""
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionOperation).where(
        TransactionOperation.tenant_id == tenant_id, TransactionOperation.work_key == work_key
    )
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


# What the provider answered, written by _record_send_evidence. A completion may record
# one in the same call that settles the row (migration 109's trigger expects exactly that:
# "a receipt requires a permit"), but it may never CHANGE one that is already there —
# whatever the earlier delivery saw is what every later read must see.
SEND_EVIDENCE_KEYS = frozenset({"receipt", "answer"})


def recorded_receipt(operation) -> dict | None:
    """The answer that proved a save, if the row holds one."""
    return (operation.result_json or {}).get("receipt")


def recorded_answer(operation) -> dict | None:
    """What the row remembers of the send: the receipt that proved a save, or the identity
    a non-receipt answer named.

    Every later read — a replayed delivery, the recovery scan — asks this one question, so
    none of them can be taught about a receipt and forget an answer.
    """
    recorded = operation.result_json or {}
    return recorded.get("receipt") or recorded.get("answer")


async def _record_send_evidence(db, tenant_id, operation_id, key, value, *, settles):
    """The one writer of what the provider answered: lock, refuse a row that moved on,
    merge under its key, audit, commit. A receipt settles the row to committed_unverified;
    an answer that proved nothing leaves the status exactly where it was."""
    evidence = _bounded_json({key: value})
    row = await _one(db, tenant_id, TransactionOperation, operation_id, lock=True)
    if row.status != "executing":
        await _commit(db, tenant_id)
        return row
    if not permit_consumed(row):
        if settles:
            raise StateError("receipt_without_permit")
        await _commit(db, tenant_id)
        return row
    row.result_json = {**(row.result_json or {}), **evidence}
    if settles:
        row.status = "committed_unverified"
        row.result_json = {**row.result_json, "termination_reason": "stall"}
    await _audit(db, tenant_id, f"operation.{key}", row, payload={key: evidence[key]})
    await _commit(db, tenant_id)
    return row


async def record_dispatch_answer(db, tenant_id, operation_id, answer, *, now=None):
    """The provider named a record but proved no save: the row keeps that identity.

    Written between the send and the readback for an answer the adapter could not call a
    receipt (indeterminate, or an error beside a record id). It is never a receipt, so the
    status does not move and nothing may be resent; it exists so a later read — this
    attempt's readback, a replayed delivery's, or the recovery scan's — still refuses an
    answer that named a different record than the approval did.
    """
    return await _record_send_evidence(db, tenant_id, operation_id, "answer", answer, settles=False)


async def record_receipt(db, tenant_id, operation_id, receipt, *, now=None):
    """The provider identified the record as saved: the attempt is committed, not yet proven.

    Written between the send and the independent readback, so the row says what is true
    if the process dies in between. Only reads may follow; the permit is already spent.
    """
    return await _record_send_evidence(db, tenant_id, operation_id, "receipt", receipt, settles=True)


async def complete_operation(db, tenant_id, operation_id, *, outcome, result_json, now=None):
    if outcome not in OUTCOMES:
        raise ValueError("Invalid operation outcome")
    evidence = _bounded_json(result_json)
    if _LEDGER_RESULT_KEYS.intersection(evidence):
        raise StateError("reserved_operation_result_key")
    row = await _one(db, tenant_id, TransactionOperation, operation_id, lock=True)
    if row.status not in IN_FLIGHT:
        raise StateError("operation_terminal")
    recorded = row.result_json or {}
    if any(key in recorded and evidence[key] != recorded[key] for key in SEND_EVIDENCE_KEYS & set(evidence)):
        # A completion may record what the provider answered; it may never rewrite it.
        raise StateError("send_evidence_immutable")
    # An unknown attempt can move only after caller-provided read-only provider
    # reconciliation; this service never dispatches it again. Handing it to a person
    # (needs_review) is an escalation, not a finding, and needs no reads.
    if row.status == "unknown" and outcome != "needs_review" and evidence.get("reconciled") is not True:
        raise StateError("reconciliation_evidence_required")
    if row.status == "committed_unverified":
        # A receipt exists, so the attempt can never be called "before effect" or
        # "unknown" again, and only an independent readback may call it verified.
        if outcome in ("rejected_before_effect", "unknown"):
            raise StateError("receipt_recorded")
        if outcome == "verified" and not isinstance(evidence.get("verification"), dict):
            raise StateError("verification_evidence_required")  # the guard trigger asks the same question
    row.status, row.completed_at = outcome, _clock(now)
    row.result_json = {**(row.result_json or {}), **evidence, "termination_reason": TERMINATION[outcome]}
    # A transaction proposal's row settles its case and records the proposal's approval; a
    # row from any other approval source (a chat confirmation) records the approval it
    # carries on itself, and its own surface owns what follows a verified outcome.
    proposal = await get_proposal(db, tenant_id, row.proposal_id) if row.proposal_id is not None else None
    if outcome == "verified" and proposal is not None:
        from app.services.transaction_ops.settlement import queue

        await queue(db, tenant_id, row, proposal, now=row.completed_at)
    approval = {"approval_kind": row.approval_kind, "approval_id": str(row.approval_id)}
    if proposal is not None:
        approval.update(
            run_id=str(proposal.run_id),
            config_id=str(proposal.config_id),
            approved_by=str(proposal.decided_by),
            approved_at=proposal.decided_at.isoformat(),
            evidence_fingerprint=proposal.evidence_fingerprint,
        )
    else:
        recorded = row.result_json or {}
        approval.update(approved_by=recorded.get("approved_by"), evidence_digest=recorded.get("evidence_digest"))
    await _audit(
        db,
        tenant_id,
        "operation.complete",
        row,
        payload={
            "outcome": outcome,
            "code": evidence.get("code"),
            **approval,
            # The result verifies the approved operation. A separate fresh case
            # observation must establish agreement on gross, tax and refunds.
            "settlement_status": "not_evaluated",
        },
    )
    await _commit(db, tenant_id)
    return row


async def reserve_operation_dispatch(
    db, tenant_id, claimed: ClaimedOperation, *, provider, payload_fingerprint, now=None, authorize=None
):
    """Consume one durable send permit. A crash after this commit permits only reads.

    An adapter calls this after its final fresh provider preflight and before
    its single mutation. No job retry or reconstructed claim can reserve again.
    Provider receipts never grant approval, reset the permit, or prove success.

    Every approval source authorizes the same way: ``authorize(db, tenant_id, operation,
    claimed, now)`` raises StateError when the approval no longer holds. A transaction
    proposal's claim brings its own (the proposal re-check); any other source must pass
    one, or no permit is minted.
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
    if claimed.proposal_id is not None:
        authorize = _proposal_still_authorized
    elif authorize is None:
        raise StateError("approval_source_required")
    operation = await _one(db, tenant_id, TransactionOperation, claimed.operation_id, lock=True)
    if (
        operation.proposal_id != claimed.proposal_id
        or operation.approval_kind != claimed.approval_kind
        or operation.approval_id != claimed.approval_id
        or operation.work_key != claimed.work_key
    ):
        raise StateError("claimed_operation_mismatch")
    if permit_consumed(operation):
        await _commit(db, tenant_id)
        return False
    if operation.status != "executing":
        raise StateError("operation_not_executable")
    if operation.provider != provider:
        raise StateError("unsupported_dispatch_provider")
    if now >= operation.deadline_at or operation.api_calls_used >= operation.max_api_calls:
        # A spent attempt is settled here, before the approval source is asked anything.
        await _exhaust_operation(db, tenant_id, operation, now, "operation_budget_exhausted")
        raise StateError("operation_budget_exhausted")
    await authorize(db, tenant_id, operation, claimed, now)
    return await _grant_permit(db, tenant_id, operation, provider, payload_fingerprint, now)


def _claimed_from_proposal(operation_id, proposal) -> ClaimedOperation:
    """The claim a transaction proposal produces; rebuilt at the permit to compare."""
    return ClaimedOperation(
        operation_id=operation_id,
        proposal_id=proposal.id,
        approval_id=proposal.id,
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


async def _proposal_still_authorized(db, tenant_id, operation, claimed, now):
    """The transaction proposal's permit-time re-check: the claim still matches the
    proposal exactly, the proposal is approved and fresh, the config still proposes
    actions, the feature is on, and the decider is still a permitted human."""
    proposal = await get_proposal(db, tenant_id, claimed.proposal_id, lock=True)
    if claimed != _claimed_from_proposal(operation.id, proposal):
        raise StateError("claimed_operation_mismatch")
    if proposal.status != "approved":
        raise StateError("operation_not_executable")
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


async def _grant_permit(db, tenant_id, operation, provider, payload_fingerprint, now):
    """The one-use permit itself: budgeted, written to the row, audited, committed. Every
    approval source's refusals run before this; nothing after it may refuse."""
    if now >= operation.deadline_at or operation.api_calls_used >= operation.max_api_calls:
        await _exhaust_operation(db, tenant_id, operation, now, "operation_budget_exhausted")
        raise StateError("operation_budget_exhausted")
    if not settings.TRANSACTION_OPS_DISPATCH_ENABLED:
        # The operator switch is the LAST refusal before the permit, so it only
        # speaks for operations that would otherwise have been sent. Anything a more
        # specific check would have refused anyway keeps that reason.
        await _block_operation(db, tenant_id, operation, now, "dispatch_disabled")
        raise StateError("dispatch_disabled")
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


async def _block_operation(db, tenant_id, operation, now, code):
    """The operator switch refused the send before the one-use permit existed.

    Nothing was sent, so this is a refusal before any effect, never an unknown:
    ``dispatch_reserved`` is never set, a duplicate delivery reads the terminal row and
    spends nothing, and the approved work needs a fresh human decision once dispatch is
    re-enabled (the taxonomy admits a lineage retry from this state). Every more specific
    refusal (provider, stale evidence, config, flags, actor, budget) runs first, so a
    ``blocked`` row always means "this would have been sent".
    """
    operation.status = "rejected_before_effect"
    operation.completed_at = now
    operation.result_json = {**(operation.result_json or {}), "termination_reason": "blocked", "code": code}
    await _audit(db, tenant_id, "operation.blocked", operation, payload={"code": code, "financial_writes": 0})
    await _commit(db, tenant_id)


async def _exhaust_operation(db, tenant_id, operation, now, code):
    # This lock is shared with the dispatch permit. An old worker cannot send
    # after recovery marks a pre-dispatch operation failed. A consumed permit
    # cannot be distinguished from a sent request, so it always stays unknown.
    if operation.status == "executing":
        # Only an executing attempt changes state on exhaustion; a settled one (a receipt
        # exists, the readback ran out of budget) keeps its state and gains the reason.
        operation.status = "unknown" if permit_consumed(operation) else "rejected_before_effect"
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
    # Reads are budgeted while the attempt is executing and, after a receipt, while it is
    # committed but unverified: the independent readback is what proves it. A send permit
    # still requires `executing` (reserve_operation_dispatch), so this never enables a resend.
    if operation.status not in OPEN:
        raise StateError("operation_not_executable")
    if now >= operation.deadline_at or operation.api_calls_used + api_calls > operation.max_api_calls:
        await _exhaust_operation(db, tenant_id, operation, now, "operation_budget_exhausted")
        return None
    if operation.proposal_id is not None:
        # The scheduled feature's flags gate a proposal's reads mid-flight. Any other
        # approval source (a chat confirmation) is gated by its own policy at the claim
        # and at the permit, not by the reconciliation product's flags.
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
    await _exhaust_operation(
        db,
        tenant_id,
        operation,
        now,
        "interrupted_after_dispatch" if permit_consumed(operation) else "interrupted_before_dispatch",
    )
    return operation


async def create_operation_recovery(db, tenant_id, operation_id, *, actor=None, evaluation_key=None, now=None):
    """One automatic pass, plus explicit, idempotent human read-only rechecks.

    Every recheck gets its own fixed run budget. No operation spend, approval,
    deadline or dispatch reservation is reset. No schedule/model may supply a
    new read-request key without a current authenticated human actor.

    A proposal's row takes its scope from the proposal; a row from another approval
    source takes it from the ``recovery_scope`` its claim recorded, never from the
    caller; without one there is no budgeted read.
    """
    now = _clock(now)
    manual = evaluation_key is not None
    if manual:
        if not isinstance(evaluation_key, uuid.UUID):
            raise ValueError("A UUID recheck key is required")
        await _human(db, tenant_id, actor, "recon.run")
    operation = await _one(db, tenant_id, TransactionOperation, operation_id, lock=True)
    key = business_digest(
        {
            "kind": "operation_recheck" if manual else "operation_recovery",
            "operation_id": operation.id,
            **({"evaluation_key": evaluation_key} if manual else {}),
        }
    )
    existing = (
        await db.execute(
            select(TransactionRun).where(TransactionRun.tenant_id == tenant_id, TransactionRun.work_key == key)
        )
    ).scalar_one_or_none()
    if existing:
        await _commit(db, tenant_id)
        return existing
    if operation.status not in SETTLED or not permit_consumed(operation):
        raise StateError("operation_not_recoverable")
    scope = (operation.result_json or {}).get("recovery_scope") or {}
    config_id = uuid.UUID(str(scope["config_id"])) if scope.get("config_id") else None
    order_reference = scope.get("order_reference")
    if (config_id is None or not order_reference) and operation.proposal_id is not None:
        # Rows claimed before the scope was recorded at the claim: the proposal still has it.
        proposal = await get_proposal(db, tenant_id, operation.proposal_id)
        config_id, order_reference = proposal.config_id, proposal.order_reference
    if config_id is None or not order_reference:
        raise StateError("recovery_unscoped")
    config = await get_config(db, tenant_id, config_id)
    if not config.enabled:
        raise StateError("config_disabled")
    pending = (
        await db.execute(
            select(TransactionRun)
            .where(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.origin == "recovery",
                TransactionRun.params_json["operation_id"].astext == str(operation.id),
                TransactionRun.status.in_(("pending", "running")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending is not None:
        if manual:
            raise StateError("recovery_already_pending")
        # The automatic collector can race a queued human request. Deliver
        # the existing check under its own lease instead of adding a budget.
        await _commit(db, tenant_id)
        return pending
    row = TransactionRun(
        tenant_id=tenant_id,
        config_id=config.id,
        work_key=key,
        origin="recovery",
        params_json={
            "operation_id": str(operation.id),
            "order_references": [order_reference],
            **({"manual_recheck": True, "evaluation_key": str(evaluation_key)} if manual else {}),
        },
        config_snapshot=_config_snapshot(config),
        max_api_calls=32,
        max_orders=1,
        deadline_at=now + timedelta(seconds=300),
        progress_json={},
        initiated_by=actor.id if manual else None,
    )
    db.add(row)
    await db.flush()
    await _audit(
        db,
        tenant_id,
        "operation.recovery.create",
        row,
        actor if manual else None,
        {"operation_id": str(operation.id), "manual_recheck": manual},
    )
    await _commit(db, tenant_id)
    return row


async def finish_operation_recovery(db, tenant_id, run_id, *, lease_token, reason, proof=None, now=None):
    """Atomically persist the recovery's terminal state and verified/unknown outcome."""
    if reason not in {"done", "budget", "stall", "error"} or (proof is not None and reason != "done"):
        raise ValueError("Invalid recovery outcome")
    now = _clock(now)
    run = await get_run(db, tenant_id, run_id, lock=True)
    if run.origin != "recovery":
        raise StateError("not_a_recovery_run")
    operation = await _one(db, tenant_id, TransactionOperation, uuid.UUID(run.params_json["operation_id"]), lock=True)
    if run.status == "finished":
        await _commit(db, tenant_id)
        return operation
    if not (
        reason == "budget" and now >= run.deadline_at and run.status == "running" and lease_token == run.lease_token
    ):
        _lease(run, lease_token, now)
    _finish(run, reason, now)
    if operation.status in SETTLED:
        # Reads only: a proof moves the row to verified; without one it keeps its state
        # (unknown stays unknown, committed_unverified stays committed_unverified).
        details = _bounded_json(
            {
                "reconciled": True,
                "recovery": {"run_id": str(run_id), "termination_reason": reason},
                **({"verification": proof} if proof is not None else {}),
            }
        )
        operation.status = "verified" if proof is not None else operation.status
        operation.completed_at = now
        operation.result_json = {
            **(operation.result_json or {}),
            **details,
            "termination_reason": "done" if proof is not None else reason,
        }
        if proof is not None and operation.proposal_id is not None:
            from app.services.transaction_ops.settlement import queue

            proposal = await get_proposal(db, tenant_id, operation.proposal_id)
            await queue(db, tenant_id, operation, proposal, now=now)
    await _audit(
        db, tenant_id, "operation.recovery.complete", operation, payload={"run_id": str(run_id), "reason": reason}
    )
    await _audit(db, tenant_id, "run.finish", run, payload={"reason": reason})
    await _commit(db, tenant_id)
    return operation
