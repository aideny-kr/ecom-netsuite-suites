"""Bounded saved-evidence pilot. No provider imports, case updates or daily receipts."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Text, cast, func, insert, select, text

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionFinding as Finding
from app.models.transaction_ops import TransactionRun as Run
from app.models.transaction_policy_replay import TransactionPolicyReplay as Replay
from app.models.transaction_policy_replay import TransactionPolicyReplayEntry as Entry
from app.services.transaction_ops import daily_evidence, policy_equivalence
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.periods import ReconciliationPolicy, review_window

MAX_CANDIDATES = 50000
BATCH_SIZE = 200


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_key: UUID
    start_date: date
    end_date: date


def utc_now():
    return datetime.now(timezone.utc)


async def get_replay(db, tenant_id, replay_id, *, lock=False):
    await set_tenant_context(db, str(tenant_id))
    query = (
        select(Replay)
        .where(Replay.tenant_id == tenant_id, Replay.id == replay_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise state.StateError("not_found", 404)
    return row


async def create_replay(db, tenant_id, config_id, request, *, actor):
    await set_tenant_context(db, str(tenant_id))
    await state._human(db, tenant_id, actor, "recon.run")
    # Config lock makes idempotency and pinning one atomic operation. Only the
    # direct recorded revision is eligible; callers cannot nominate a baseline.
    target = await state.get_config(db, tenant_id, config_id, lock=True)
    existing = await db.scalar(
        select(Replay).where(Replay.tenant_id == tenant_id, Replay.evaluation_key == request.evaluation_key)
    )
    request_json = request.model_dump(mode="json")
    if existing:
        if existing.target_config_id != config_id or existing.request_json != request_json:
            raise state.StateError("evaluation_key_conflict", 409)
        return existing
    pending = await db.scalar(
        select(Replay.id)
        .where(Replay.tenant_id == tenant_id, Replay.target_config_id == config_id, Replay.status == "pending")
        .limit(1)
    )
    if pending:
        raise state.StateError("policy_replay_already_pending", 409)
    if not target.enabled or not target.supersedes_config_id:
        raise state.StateError("policy_revision_required", 409)
    source = await state.get_config(db, tenant_id, target.supersedes_config_id)
    before, after = state._config_snapshot(source), state._config_snapshot(target)
    try:
        changed = policy_equivalence.changed_reasons(before, after)
        policy = ReconciliationPolicy.model_validate(after["mapping_json"].get("reconciliation_policy", {}))
        window = review_window(
            "custom",
            utc_now(),
            policy.timezone_name,
            start_date=request.start_date,
            end_date=request.end_date,
            basis=policy.review_basis,
        )
    except ValueError as exc:
        raise state.StateError("policy_replay_unsupported", 422) from exc
    root = SimpleNamespace(
        tenant_id=tenant_id, config_id=source.id, config_snapshot=before, params_json=window, origin="manual"
    )
    span = SimpleNamespace(start=window["window_start"], end=window["window_end"])
    # Read bounded original coverage separately. It describes provider-backed
    # scans under the OLD policy, never a new scan or complete population proof.
    await db.execute(text("SET LOCAL statement_timeout = '15s'"))
    windows = await daily_evidence.completed_observation_windows(db, root, span)
    candidates = (
        select(
            Finding.id.label("finding_id"),
            Finding.run_id,
            Finding.order_reference,
            func.encode(func.sha256(func.convert_to(cast(Finding.report_json, Text), "UTF8")), "hex").label(
                "original_hash"
            ),
        )
        .join(Run, (Run.tenant_id == Finding.tenant_id) & (Run.id == Finding.run_id))
        .where(
            Finding.tenant_id == tenant_id,
            *daily_evidence.compatible_observation_runs(root, span),
            Run.status == "finished",
        )
        .distinct(Finding.order_reference)
        .order_by(Finding.order_reference, Finding.updated_at.desc(), Finding.id.desc())
        .limit(MAX_CANDIDATES + 1)
    )
    pinned = (await db.execute(candidates)).mappings().all()
    if len(pinned) > MAX_CANDIDATES:
        raise state.StateError("policy_replay_population_limit", 422)
    replay = Replay(
        id=uuid4(),
        tenant_id=tenant_id,
        evaluation_key=request.evaluation_key,
        source_config_id=source.id,
        target_config_id=target.id,
        initiated_by=actor.id,
        request_json=request_json,
        source_snapshot=before,
        target_snapshot=after,
        status="pending",
        processed=0,
        manifest_json={
            "rule_version": policy_equivalence.RULE_VERSION,
            "changed_reason_ids": sorted(changed),
            "window": {
                key: value.isoformat() if isinstance(value, datetime) else value for key, value in window.items()
            },
            "candidate_count": len(pinned),
            "population_completeness": "unverified",
            "population_note": (
                "Latest retained finding per order in original scan windows; "
                "undiscovered or unrecorded orders are not certified."
            ),
            "original_scan_coverage_complete": daily_evidence.covered_until(span.start, span.end, windows) == span.end,
            "original_scan_run_ids": [value[2] for value in windows],
            "comparison_basis": "historical_as_observed",
            "daily_coverage_advanced": False,
            "financial_certification": "not_certified",
            "provider_calls": 0,
            "jev_calls": 0,
        },
    )
    db.add(replay)
    await db.flush()
    # Batch metadata inserts: no per-order commits, full source payload copies,
    # provider reads, or locks on the existing reconciliation cases/runs.
    for start in range(0, len(pinned), 1000):
        await db.execute(
            insert(Entry),
            [{"tenant_id": tenant_id, "replay_id": replay.id, **dict(row)} for row in pinned[start : start + 1000]],
        )
    await state._audit(
        db,
        tenant_id,
        "policy_replay.created",
        replay,
        actor=actor,
        payload={"candidate_count": len(pinned), "target_config_id": str(config_id)},
    )
    await state._commit(db, tenant_id)
    return replay


async def process_batch(db, tenant_id, replay_id):
    """Result and progress commit together. Retrying a crashed batch is harmless."""
    replay = await get_replay(db, tenant_id, replay_id, lock=True)
    await db.execute(text("SET LOCAL statement_timeout = '15s'"))
    if replay.status != "pending":
        return replay.status
    if replay.manifest_json["rule_version"] != policy_equivalence.RULE_VERSION:
        raise state.StateError("policy_replay_rule_changed", 409)
    await state._human(db, tenant_id, SimpleNamespace(id=replay.initiated_by, tenant_id=tenant_id), "recon.run")
    replay.last_error_code = None
    changed = policy_equivalence.changed_reasons(replay.source_snapshot, replay.target_snapshot)
    rows = (
        await db.execute(
            select(
                Entry,
                Finding.report_json,
                func.encode(func.sha256(func.convert_to(cast(Finding.report_json, Text), "UTF8")), "hex"),
                Run.config_snapshot,
            )
            .join(Finding, (Finding.tenant_id == Entry.tenant_id) & (Finding.id == Entry.finding_id))
            .join(Run, (Run.tenant_id == Entry.tenant_id) & (Run.id == Entry.run_id))
            .where(Entry.tenant_id == tenant_id, Entry.replay_id == replay_id, Entry.result_json.is_(None))
            .order_by(Entry.order_reference)
            .limit(BATCH_SIZE)
        )
    ).all()
    now = utc_now()
    for entry, report, current_hash, original_snapshot in rows:
        result = {"status": "unknown", "reason": "original_evidence_changed"}
        if current_hash == entry.original_hash:
            # Missing legacy versions retain the explicit legacy contract; an
            # older discovery contract cannot silently inherit a newer one.
            original_snapshot = {
                "evidence_contract_version": 1,
                "destination_discovery_version": 1,
                **original_snapshot,
            }
            try:
                observed_changed = policy_equivalence.changed_reasons(original_snapshot, replay.target_snapshot)
                result = policy_equivalence.evaluate(report, original_snapshot, observed_changed, evaluated_at=now)
            except ValueError:
                result = {"status": "unknown", "reason": "original_contract_incompatible"}
        entry.result_json = {
            **result,
            "rule_version": policy_equivalence.RULE_VERSION,
            "changed_reason_ids": sorted(changed),
        }
        entry.evaluated_at = now
    replay.processed += len(rows)
    if replay.processed == replay.manifest_json["candidate_count"]:
        replay.status = "finished"
        replay.finished_at = now
        await state._audit(db, tenant_id, "policy_replay.finished", replay, payload={"processed": replay.processed})
    elif not rows:
        raise state.StateError("policy_replay_source_missing", 409)
    await state._commit(db, tenant_id)
    return replay.status


async def status(db, tenant_id, replay_id):
    replay = await get_replay(db, tenant_id, replay_id)
    outcome = Entry.result_json["status"].astext
    counts = dict(
        (
            await db.execute(
                select(outcome, func.count())
                .where(
                    Entry.tenant_id == tenant_id,
                    Entry.replay_id == replay_id,
                )
                .group_by(outcome)
            )
        ).all()
    )
    return {
        "id": str(replay.id),
        "status": replay.status,
        "created_at": replay.created_at,
        "finished_at": replay.finished_at,
        "source_config_id": str(replay.source_config_id),
        "target_config_id": str(replay.target_config_id),
        "processed": replay.processed,
        "counts": {key: counts.get(key, 0) for key in ("equivalent", "affected", "unknown")},
        "pending": counts.get(None, 0),
        "last_error_code": replay.last_error_code,
        "manifest": replay.manifest_json,
    }


async def entries(db, tenant_id, replay_id, *, after="", limit=200, outcome=None):
    await get_replay(db, tenant_id, replay_id)
    query = select(Entry).where(
        Entry.tenant_id == tenant_id, Entry.replay_id == replay_id, Entry.order_reference > after
    )
    if outcome:
        query = query.where(Entry.result_json["status"].astext == outcome)
    rows = (await db.scalars(query.order_by(Entry.order_reference).limit(limit))).all()
    return [
        {
            "order_reference": row.order_reference,
            "finding_id": str(row.finding_id),
            "run_id": str(row.run_id),
            "original_hash": row.original_hash,
            "evaluated_at": row.evaluated_at,
            "result": row.result_json,
        }
        for row in rows
    ]


async def cancel(db, tenant_id, replay_id, *, actor):
    await set_tenant_context(db, str(tenant_id))
    await state._human(db, tenant_id, actor, "recon.run")
    replay = await get_replay(db, tenant_id, replay_id, lock=True)
    if replay.status == "pending":
        replay.status = "cancelled"
        replay.finished_at = utc_now()
        await state._audit(db, tenant_id, "policy_replay.cancelled", replay, actor=actor)
        await state._commit(db, tenant_id)
    return replay


async def publish(tenant_id, replay_id):
    import asyncio

    from app.workers.tasks.transaction_ops import transaction_policy_replay

    try:
        await asyncio.wait_for(
            asyncio.to_thread(transaction_policy_replay.delay, str(tenant_id), str(replay_id)), timeout=3
        )
        return True
    except Exception:
        # Durable pending work remains resumable through the same authenticated
        # endpoint; never report that broker acceptance completed evaluation.
        return False


async def record_failure(db, tenant_id, replay_id):
    replay = await get_replay(db, tenant_id, replay_id, lock=True)
    if replay.status == "pending":
        replay.last_error_code = "policy_replay_failed"
        await state._commit(db, tenant_id)
