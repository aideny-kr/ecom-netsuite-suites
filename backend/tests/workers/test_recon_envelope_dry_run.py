"""Rung-1 dry run: evaluates the autonomy envelope on the tenant's latest
COMPLETED run and writes exactly ONE system-actor audit event. It must NEVER
mutate result rows (report-only) and must skip when the flag is off."""

from decimal import Decimal

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.services import feature_flag_service
from app.workers.tasks.recon_envelope_dry_run import DRY_RUN_ACTION, dry_run_for_tenant
from tests.conftest import create_test_recon_result, create_test_recon_run


async def _audit_rows(db, run_id):
    return (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == DRY_RUN_ACTION,
                    AuditEvent.resource_id == str(run_id),
                )
            )
        )
        .scalars()
        .all()
    )


async def test_flag_off_skips_and_writes_nothing(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))

    assert out["skipped"] == "flag_disabled"
    assert await _audit_rows(db, run.id) == []


async def test_writes_one_system_audit_and_mutates_nothing(db, tenant_a):
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    candidate = await create_test_recon_result(db, tenant_a.id, run.id, status="suggested")
    excluded = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="suggested",
        match_type="fuzzy",
        variance_amount=Decimal("5.00"),
        variance_type="fees",
    )
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))

    assert out["run_id"] == str(run.id)
    assert out["candidate_count"] == 1
    events = await _audit_rows(db, run.id)
    assert len(events) == 1
    evt = events[0]
    assert evt.actor_type == "system"
    assert evt.actor_id is None
    assert evt.category == "reconciliation"
    assert evt.payload["candidate_count"] == 1
    assert evt.payload["candidate_ids"] == [str(candidate.id)]
    # report-only invariant: statuses untouched
    await db.refresh(candidate)
    await db.refresh(excluded)
    assert candidate.status == "suggested"
    assert excluded.status == "suggested"


async def test_no_completed_run_skips(db, tenant_a):
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    await create_test_recon_run(db, tenant_a.id, status="running")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))
    assert out["skipped"] == "no_completed_run"


def test_tasks_registered_on_recon_queue():
    from app.workers.tasks.recon_envelope_dry_run import (
        recon_envelope_dry_run,
        recon_envelope_dry_run_all,
    )

    assert recon_envelope_dry_run.name == "tasks.recon_envelope_dry_run"
    assert recon_envelope_dry_run_all.name == "tasks.recon_envelope_dry_run_all"
    assert recon_envelope_dry_run.queue == "recon"
    assert recon_envelope_dry_run_all.queue == "recon"


def test_include_and_beat_wiring():
    from app.workers.celery_app import celery_app

    assert "app.workers.tasks.recon_envelope_dry_run" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule["recon-envelope-dry-run-nightly"]
    assert entry["task"] == "tasks.recon_envelope_dry_run_all"
