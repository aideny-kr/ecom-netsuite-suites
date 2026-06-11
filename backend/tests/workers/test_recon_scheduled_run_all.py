"""Nightly scheduled recon fan-out: dispatches the existing
tasks.reconciliation_run per recon_scheduled_runs-enabled tenant.
Read+match only — no approvals, no NetSuite writes."""

from datetime import date, timedelta

from app.services import feature_flag_service


def test_is_celery_task_on_recon_queue():
    from app.workers.tasks.recon_scheduled_run_all import recon_scheduled_run_all

    assert hasattr(recon_scheduled_run_all, "delay")
    assert recon_scheduled_run_all.name == "tasks.recon_scheduled_run_all"
    assert recon_scheduled_run_all.queue == "recon"


async def test_dispatches_only_enabled_tenants(db, tenant_a, tenant_b, monkeypatch):
    from app.workers.tasks import recon_scheduled_run_all as mod

    await feature_flag_service.set_flag(db, tenant_a.id, "recon_scheduled_runs", True)
    await feature_flag_service.set_flag(db, tenant_b.id, "recon_scheduled_runs", False)
    await db.commit()

    sent: list[dict] = []
    monkeypatch.setattr(
        mod.celery_app,
        "send_task",
        lambda name, kwargs=None, queue=None, **_: sent.append({"name": name, "kwargs": kwargs, "queue": queue}),
    )
    # collect_and_dispatch takes the session directly — no session-factory patching needed.
    stats = await mod.collect_and_dispatch(db)

    assert stats == {"dispatched": 1, "failed": 0}
    assert len(sent) == 1
    assert sent[0]["name"] == "tasks.reconciliation_run"
    assert sent[0]["queue"] == "recon"
    assert sent[0]["kwargs"]["tenant_id"] == str(tenant_a.id)
    expected_from = (date.today() - timedelta(days=mod.SCHEDULED_RUN_WINDOW_DAYS)).isoformat()
    assert sent[0]["kwargs"]["date_from"] == expected_from
    assert sent[0]["kwargs"]["date_to"] == date.today().isoformat()
