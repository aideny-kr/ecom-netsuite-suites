"""Rung-1 dry run: evaluates the autonomy envelope on the tenant's latest
COMPLETED run and writes exactly ONE system-actor audit event. It must NEVER
mutate result rows (report-only) and must skip when the flag is off."""

from decimal import Decimal
from pathlib import Path

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
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))

    assert out["skipped"] == "flag_disabled"
    assert await _audit_rows(db, run.id) == []


async def test_master_reconciliation_flag_off_skips(db, tenant_a):
    """autonomous_recon alone is not enough — the master `reconciliation`
    feature gate must also be on (the user-facing recon surface enforces it
    via require_feature; the scheduled path must not bypass it)."""
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))

    assert out["skipped"] == "reconciliation_disabled"
    assert await _audit_rows(db, run.id) == []


async def test_already_evaluated_run_is_not_reaudited(db, tenant_a):
    """A run gets ONE dry-run audit event ever — a tenant with no new runs must
    not be re-audited nightly forever."""
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await create_test_recon_result(db, tenant_a.id, run.id, status="suggested")
    await db.flush()

    first = await dry_run_for_tenant(db, str(tenant_a.id))
    second = await dry_run_for_tenant(db, str(tenant_a.id))

    assert first["evaluated_count"] == 1
    assert second["evaluated_count"] == 0
    assert second["already_evaluated_count"] == 1
    assert len(await _audit_rows(db, run.id)) == 1


async def test_catchup_cap_does_not_starve_older_unevaluated_runs(db, tenant_a, monkeypatch):
    """Codex P2: the per-pass cap must apply AFTER filtering audited runs —
    otherwise a busy tenant's newest N audited runs occupy the window forever
    and older unevaluated runs are never reached."""
    from app.workers.tasks import recon_envelope_dry_run as mod

    monkeypatch.setattr(mod, "MAX_RUNS_PER_EVALUATION", 1)
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    older = await create_test_recon_run(db, tenant_a.id, status="completed")
    newer = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    first = await dry_run_for_tenant(db, str(tenant_a.id))
    second = await dry_run_for_tenant(db, str(tenant_a.id))

    assert first["evaluated_count"] == 1
    assert second["evaluated_count"] == 1  # the OLDER run, not a re-skip of the newer
    assert len(await _audit_rows(db, newer.id)) == 1
    assert len(await _audit_rows(db, older.id)) == 1


async def test_catches_up_on_all_unevaluated_runs(db, tenant_a):
    """Coverage must not depend on Beat timing: a run superseded by a newer one
    before its first evaluation is still picked up (catch-up over the recent
    window, newest first), each with its own audit event."""
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    older = await create_test_recon_run(db, tenant_a.id, status="completed")
    newer = await create_test_recon_run(db, tenant_a.id, status="completed")
    await create_test_recon_result(db, tenant_a.id, older.id, status="suggested")
    await create_test_recon_result(db, tenant_a.id, newer.id, status="suggested")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))

    assert out["evaluated_count"] == 2
    assert out["already_evaluated_count"] == 0
    assert len(await _audit_rows(db, older.id)) == 1
    assert len(await _audit_rows(db, newer.id)) == 1


async def test_writes_one_system_audit_and_mutates_nothing(db, tenant_a):
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
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

    assert out["evaluated_count"] == 1
    assert out["reports"][0]["run_id"] == str(run.id)
    assert out["reports"][0]["candidate_count"] == 1
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


async def test_results_query_is_tenant_scoped(db, tenant_a, tenant_b):
    """A qualifying result row belonging to ANOTHER tenant (but attached to this
    tenant's run) must never be counted as a candidate — the results query has
    to filter on tenant_id, not run_id alone."""
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    # Foreign-tenant row pointing at tenant_a's run — would qualify if counted.
    await create_test_recon_result(db, tenant_b.id, run.id, status="suggested")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))

    assert out["reports"][0]["run_id"] == str(run.id)
    assert out["reports"][0]["candidate_count"] == 0


async def test_no_completed_run_skips(db, tenant_a):
    feature_flag_service.clear_cache()
    await feature_flag_service.set_flag(db, tenant_a.id, "reconciliation", True)
    await feature_flag_service.set_flag(db, tenant_a.id, "autonomous_recon", True)
    await create_test_recon_run(db, tenant_a.id, status="running")
    await db.flush()

    out = await dry_run_for_tenant(db, str(tenant_a.id))
    assert out["skipped"] == "no_completed_run"


def test_uses_worker_async_session_not_module_factory():
    """Both task wrappers must use worker_async_session (disposable per-task
    engine) — never the module-level async_session_factory, which is bound to
    the parent process's event loop (prefork-unsafe). Source-inspection, same
    style as tests/test_worker_rls.py."""
    backend_root = Path(__file__).resolve().parent.parent.parent
    with open(backend_root / "app/workers/tasks/recon_envelope_dry_run.py") as f:
        src = f.read()
    assert "worker_async_session" in src
    assert "async_session_factory" not in src


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
