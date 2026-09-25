from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.models.audit import AuditEvent
from app.models.feature_flag import TenantFeatureFlag
from app.models.transaction_ops import TransactionCase, TransactionCaseObservation, TransactionFinding
from app.schemas.transaction_runs import ProgressUpdate, RunCreate
from app.services.transaction_ops import finding_batch
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_planner import planning_case
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture
async def batch_setup(db, admin_user):
    actor, _ = admin_user
    case = planning_case()
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3", mapping_json=case.config.mapping_json)
    refs = [f"R12345678{i}" for i in range(3)]
    run = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key="chunk", order_references=refs), actor=actor
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    await state.update_progress(
        db,
        actor.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"pending_refs": refs, "processed": 0}),
        lease_token=token,
    )
    reports = []
    for ref in refs:
        report = deepcopy(case.report)
        report["order_reference"] = report["source"]["order_reference"] = ref
        for target in report["targets"]:
            target["order_reference"] = ref
        reports.append(report)
    return actor, config, run, token, refs, reports


async def publish(db, setup, *, reports=None, checkpoint=None, now=None):
    actor, _, run, token, refs, original = setup
    return await state.record_finding_batch(
        db,
        actor.tenant_id,
        run.id,
        reports if reports is not None else original,
        lease_token=token,
        checkpoint=checkpoint or ProgressUpdate(progress_json={"pending_refs": [], "processed": len(refs)}),
        now=now,
    )


async def count(db, tenant, model):
    return await db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant))


async def test_batch_matches_single_writer_and_preserves_ids_and_idempotent_observations(db, batch_setup):
    actor, _, run, token, refs, reports = batch_setup
    now = datetime.now(timezone.utc)
    expected = {}
    for ref, report in zip(refs, reports):
        row = await state.record_finding(db, actor.tenant_id, run.id, ref, report, lease_token=token, now=now)
        expected[ref] = (row.id, deepcopy(row.report_json))
    audit_count = await count(db, actor.tenant_id, AuditEvent)
    rows = await publish(db, batch_setup, now=now)
    assert {r.order_reference: (r.id, r.report_json) for r in rows} == expected
    assert await count(db, actor.tenant_id, TransactionCaseObservation) == 3
    assert await count(db, actor.tenant_id, AuditEvent) == audit_count
    assert (await state.get_run(db, actor.tenant_id, run.id)).progress_json == {"pending_refs": [], "processed": 3}


async def test_new_batch_creates_all_cases_history_and_per_order_audits(db, batch_setup):
    actor, _, run, _, _, _ = batch_setup
    rows = await publish(db, batch_setup)
    assert len(rows) == 3
    assert await count(db, actor.tenant_id, TransactionCase) == 3
    assert await count(db, actor.tenant_id, TransactionCaseObservation) == 3
    audits = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.action == "transaction_ops.case.evaluated",
            )
        )
    )
    assert len(audits) == 3
    assert all(e.payload["run_id"] == str(run.id) and e.payload["became_current"] for e in audits)
    assert all(r.report_json["case_id"] for r in rows)


async def test_failed_batch_leaves_no_finding_or_cursor_and_can_retry(db, batch_setup, monkeypatch):
    actor, _, run, _, refs, _ = batch_setup
    run_id, tenant = run.id, actor.tenant_id
    persist = finding_batch.persist

    async def fail(*args, **kwargs):
        await persist(*args, **kwargs)
        raise RuntimeError("injected failure before checkpoint commit")

    monkeypatch.setattr(finding_batch, "persist", fail)
    with pytest.raises(RuntimeError):
        await publish(db, batch_setup)
    await db.rollback()
    restored = await state.get_run(db, tenant, run_id)
    assert restored.progress_json == {"pending_refs": refs, "processed": 0}
    assert await count(db, tenant, TransactionFinding) == 0
    assert await count(db, tenant, TransactionCaseObservation) == 0
    assert await count(db, tenant, TransactionCase) == 0
    monkeypatch.setattr(finding_batch, "persist", persist)
    # rollback expires fixture ORM objects; use captured scalar identities.
    from types import SimpleNamespace

    setup = (SimpleNamespace(tenant_id=tenant), None, restored, restored.lease_token, refs, batch_setup[-1])
    assert len(await publish(db, setup)) == 3


@pytest.mark.parametrize("failure", ["noncontiguous", "counter", "duplicate", "lease", "config", "feature", "tenant"])
async def test_invalid_or_revoked_batch_never_advances(db, batch_setup, failure):
    actor, config, run, token, refs, reports = batch_setup
    options = {}
    if failure == "noncontiguous":
        options["reports"] = reports[::-1]
    elif failure == "counter":
        options["checkpoint"] = ProgressUpdate(progress_json={"pending_refs": [], "processed": 100})
    elif failure == "duplicate":
        options["reports"] = [reports[0], reports[0]]
    elif failure == "lease":
        batch_setup = (actor, config, run, uuid4(), refs, reports)
    elif failure == "config":
        config.enabled = False
        await db.flush()
    elif failure == "feature":
        await db.execute(
            update(TenantFeatureFlag)
            .where(
                TenantFeatureFlag.tenant_id == actor.tenant_id,
                TenantFeatureFlag.flag_key == "reconciliation",
            )
            .values(enabled=False)
        )
    else:
        from types import SimpleNamespace

        batch_setup = (SimpleNamespace(tenant_id=uuid4()), config, run, token, refs, reports)
    with pytest.raises((ValueError, state.StateError)):
        await publish(db, batch_setup, **options)
    assert await count(db, actor.tenant_id, TransactionFinding) == 0
    assert run.progress_json["pending_refs"] == refs


async def test_batch_reopens_cases_and_older_evidence_does_not_replace_newer(db, batch_setup):
    actor, _, run, token, refs, reports = batch_setup
    now = datetime.now(timezone.utc)
    await publish(db, batch_setup, now=now)
    cases = list(await db.scalars(select(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id)))
    for case in cases:
        case.status = "reconciled"
    await db.flush()
    await state.update_progress(
        db,
        actor.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"pending_refs": refs, "processed": 0}),
        lease_token=token,
    )
    changed = deepcopy(reports)
    for report in changed:
        report["reason_for_test"] = "new observation"
    await publish(db, batch_setup, reports=changed, now=now + timedelta(seconds=1))
    assert all(case.status == "open" for case in cases)
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.action == "transaction_ops.case.reopened",
            )
        )
        == 3
    )
    newer_reports = [deepcopy(c.latest_report_json) for c in cases]
    await state.update_progress(
        db,
        actor.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"pending_refs": refs, "processed": 0}),
        lease_token=token,
    )
    for report in changed:
        report["source"]["observed_at"] = (now - timedelta(hours=1)).isoformat()
    await publish(db, batch_setup, reports=changed, now=now + timedelta(seconds=2))
    assert [c.latest_report_json for c in cases] == newer_reports


async def test_unsupported_lifecycle_uses_existing_writer(db, batch_setup, monkeypatch):
    spy = AsyncMock(wraps=state._record_finding)
    monkeypatch.setattr(state, "_record_finding", spy)
    monkeypatch.setattr(finding_batch, "eligible", lambda *args: False)
    await publish(db, batch_setup)
    assert spy.await_count == 3


async def test_batch_reduces_database_roundtrips_for_complete_case_history(db, batch_setup, record_property):
    from sqlalchemy import event

    actor, _, run, token, refs, reports = batch_setup
    now = datetime.now(timezone.utc)
    # Both measured paths update existing findings/cases and append new history.
    for ref, report in zip(refs, reports):
        await state.record_finding(db, actor.tenant_id, run.id, ref, report, lease_token=token, now=now)
    calls = []

    def query(*args):
        calls.append(1)

    event.listen(db.bind.sync_engine, "before_cursor_execute", query)
    try:
        for ref, report in zip(refs, reports):
            await state.record_finding(
                db,
                actor.tenant_id,
                run.id,
                ref,
                {**report, "probe": "serial"},
                lease_token=token,
                now=now + timedelta(seconds=1),
            )
        serial = len(calls)
        calls.clear()
        await publish(
            db,
            batch_setup,
            reports=[{**report, "probe": "batch"} for report in reports],
            now=now + timedelta(seconds=2),
        )
        batched = len(calls)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", query)
    record_property("serial_sql_calls", serial)
    record_property("batch_sql_calls", batched)
    assert batched <= serial * 0.75
    assert await count(db, actor.tenant_id, TransactionCaseObservation) == 9
