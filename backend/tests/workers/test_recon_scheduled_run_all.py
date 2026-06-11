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
    # Scheduled runs MUST use the product-default order-level engine (OrderReconJob),
    # which carries all the R1/R2 hardening — not the legacy payout-level runner.
    assert sent[0]["kwargs"]["match_level"] == "order"


class _FakeSummary:
    def model_dump(self, mode="json"):
        return {"ok": True}


async def test_reconciliation_run_routes_match_level(monkeypatch):
    """The task's inner logic routes order→OrderReconJob, payout→ReconJobRunner."""
    from app.workers.tasks import reconciliation_run as mod

    instantiated: list[str] = []

    class FakeOrderJob:
        def __init__(self, db, tenant_id):
            instantiated.append("order")

        async def run(self, date_from, date_to, subsidiary_id=None, job_id=None):
            assert date_from == date(2026, 6, 1)  # ISO string parsed to date
            return _FakeSummary()

    class FakeRunner:
        def __init__(self, db, tenant_id):
            instantiated.append("payout")

        async def run(self, date_from, date_to, subsidiary_id=None, payout_ids=None, job_id=None):
            assert payout_ids == ["po_1"]
            return _FakeSummary()

    monkeypatch.setattr("app.services.reconciliation.order_recon_job.OrderReconJob", FakeOrderJob)
    monkeypatch.setattr("app.services.reconciliation.recon_job.ReconJobRunner", FakeRunner)

    common = dict(
        db=object(),
        tenant_id="t1",
        date_from="2026-06-01",
        date_to="2026-06-07",
        subsidiary_id=None,
        job_id=None,
    )
    out = await mod._execute(payout_ids=None, match_level="order", **common)
    assert out == {"ok": True}
    assert instantiated == ["order"]

    instantiated.clear()
    out = await mod._execute(payout_ids=["po_1"], match_level="payout", **common)
    assert out == {"ok": True}
    assert instantiated == ["payout"]


async def test_reconciliation_run_task_defaults_to_order_level():
    """match_level defaults to 'order' (the product default) on the task itself."""
    import inspect

    from app.workers.tasks.reconciliation_run import reconciliation_run_task

    sig = inspect.signature(reconciliation_run_task.run)
    assert sig.parameters["match_level"].default == "order"


def test_include_and_beat_wiring():
    from app.workers.celery_app import celery_app

    # tasks.reconciliation_run was previously DEAD (defined but unregistered) —
    # the fan-out dispatches it by name, so it MUST be in include.
    assert "app.workers.tasks.reconciliation_run" in celery_app.conf.include
    assert "app.workers.tasks.recon_scheduled_run_all" in celery_app.conf.include

    entry = celery_app.conf.beat_schedule["recon-scheduled-run-nightly"]
    assert entry["task"] == "tasks.recon_scheduled_run_all"
