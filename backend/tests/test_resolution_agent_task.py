"""ResolutionAgent Celery task + flag-gated dispatch after planning.

Uses the FakeAdapter (mirrors LLMResponse/ToolUseBlock) monkeypatched over
get_adapter, and a monkeypatched get_tenant_ai_config, so the task's inner
async function (run_resolution_agent) can be driven directly against the
seeded test db session — no real Celery worker, no network call.
"""

from decimal import Decimal

from sqlalchemy import select

from app.api.v1.reconciliation import plan_resolutions
from app.models.reconciliation import ReconResolutionProposal
from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.reconciliation.resolution_planner import plan_run
from app.workers.tasks import recon_resolution_agent as agent_task
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


class FakeAdapter:
    """Canned single-tool-use response — mirrors the real adapter's return shape."""

    def __init__(self, action: str, narrative: str, key_evidence: list[str] | None = None):
        self.action = action
        self.narrative = narrative
        self.key_evidence = key_evidence or []
        self.calls: list[dict] = []

    async def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            tool_use_blocks=[
                ToolUseBlock(
                    id="tu_1",
                    name="classify_resolution",
                    input={
                        "action": self.action,
                        "narrative": self.narrative,
                        "key_evidence": self.key_evidence,
                    },
                )
            ]
        )


async def _seed_planned_run(db, tenant_id):
    run = await create_test_recon_run(db, tenant_id, status="completed")
    result = await create_test_recon_result(
        db,
        tenant_id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="manual_adjustment",
        variance_amount=Decimal("77.10"),
        stripe_amount=Decimal("500.00"),
        netsuite_amount=Decimal("422.90"),
        evidence={"charge_source_id": "ch_task1", "order_reference": "R628489275"},
    )
    await db.flush()
    await plan_run(db, tenant_id, run.id)
    return run, result


async def test_task_processes_seeded_run_end_to_end(db, tenant_a, monkeypatch):
    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run, _result = await _seed_planned_run(db, tenant_a.id)

    fake_adapter = FakeAdapter(
        action="book_fee_line",
        narrative="Unexplained variance of $77.10 against a $500.00 charge — book as a fee line.",
        key_evidence=["variance_amount=77.10"],
    )

    async def fake_get_tenant_ai_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: fake_adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_get_tenant_ai_config)

    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))

    assert summary == {
        "processed": 1,
        "upgraded": 1,
        "kept_needs_human": 0,
        "contract_violations": 0,
    }

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    planner_rows = [p for p in props if p.source == "planner"]
    agent_rows = [p for p in props if p.source == "agent"]
    assert len(planner_rows) == 1
    assert planner_rows[0].status == "superseded"
    assert len(agent_rows) == 1
    assert agent_rows[0].status == "proposed"
    assert agent_rows[0].action == "book_fee_line"


async def test_task_skips_when_agent_flag_disabled(db, tenant_a, monkeypatch):
    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    # recon_resolution_agent left at its default OFF.
    run, _result = await _seed_planned_run(db, tenant_a.id)

    called = {"adapter": False}

    def _fake_get_adapter(*_a, **_kw):
        called["adapter"] = True
        return FakeAdapter(action="book_fee_line", narrative="n/a")

    monkeypatch.setattr(agent_task, "get_adapter", _fake_get_adapter)

    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))

    assert summary == {"skipped": "flag_disabled"}
    assert called["adapter"] is False

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    assert all(p.status == "proposed" for p in props)
    assert all(p.source == "planner" for p in props)


async def test_plan_resolutions_dispatches_agent_when_flag_on(db, tenant_a, monkeypatch):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    called = {}

    def _fake_dispatch(tenant_id, run_id):
        called["tenant_id"] = tenant_id
        called["run_id"] = run_id

    monkeypatch.setattr("app.api.v1.reconciliation.dispatch_resolution_agent", _fake_dispatch, raising=False)

    await plan_resolutions(str(run.id), user=user, db=db)

    assert called == {"tenant_id": str(user.tenant_id), "run_id": str(run.id)}


async def test_plan_resolutions_does_not_dispatch_when_agent_flag_off(db, tenant_a, monkeypatch):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    # recon_resolution_agent left OFF.
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    called = {"count": 0}

    def _fake_dispatch(tenant_id, run_id):
        called["count"] += 1

    monkeypatch.setattr("app.api.v1.reconciliation.dispatch_resolution_agent", _fake_dispatch, raising=False)

    await plan_resolutions(str(run.id), user=user, db=db)

    assert called["count"] == 0


async def test_hook_dispatches_when_agent_flag_on(db, tenant_a, monkeypatch):
    """Mirrors test_resolution_plan_hook.py's hook-driving pattern."""
    from datetime import date

    from app.schemas.order_reconciliation import ChargeRecord, OrderMatchCandidate
    from app.services.reconciliation.order_recon_job import OrderReconJob

    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")

    charge = ChargeRecord(
        id="pl-1",
        source_id="ch_hook_task5",
        payout_line_id="pl-1",
        amount=Decimal("50.00"),
        fee=Decimal("1.50"),
        net=Decimal("48.50"),
        currency="USD",
        charge_date=date(2026, 3, 15),
    )
    unmatched_candidate = OrderMatchCandidate(
        charge=charge,
        deposit=None,
        match_type="unmatched",
        confidence=Decimal("0"),
        variance_amount=Decimal("50.00"),
        variance_type="missing",
    )

    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))

    called = {"count": 0}

    def _fake_dispatch(tenant_id, run_id):
        called["count"] += 1
        called["tenant_id"] = tenant_id
        called["run_id"] = run_id

    from unittest.mock import patch

    with (
        patch.object(job, "_fetch_charges", return_value=[charge]),
        patch.object(job, "_fetch_deposits", return_value=[]),
        patch.object(job.engine, "match", return_value=[unmatched_candidate]),
        patch(
            "app.services.reconciliation.order_recon_job.dispatch_resolution_agent",
            side_effect=_fake_dispatch,
        ),
    ):
        summary = await job.run(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

    assert called["count"] == 1
    assert called["tenant_id"] == str(tenant_a.id)
    assert called["run_id"] == summary.run_id


async def test_hook_does_not_dispatch_when_agent_flag_off(db, tenant_a):
    from datetime import date

    from app.schemas.order_reconciliation import ChargeRecord, OrderMatchCandidate
    from app.services.reconciliation.order_recon_job import OrderReconJob

    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    # recon_resolution_agent left OFF.

    charge = ChargeRecord(
        id="pl-1",
        source_id="ch_hook_task5b",
        payout_line_id="pl-1",
        amount=Decimal("50.00"),
        fee=Decimal("1.50"),
        net=Decimal("48.50"),
        currency="USD",
        charge_date=date(2026, 3, 15),
    )
    unmatched_candidate = OrderMatchCandidate(
        charge=charge,
        deposit=None,
        match_type="unmatched",
        confidence=Decimal("0"),
        variance_amount=Decimal("50.00"),
        variance_type="missing",
    )

    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))

    called = {"count": 0}

    def _fake_dispatch(tenant_id, run_id):
        called["count"] += 1

    from unittest.mock import patch

    with (
        patch.object(job, "_fetch_charges", return_value=[charge]),
        patch.object(job, "_fetch_deposits", return_value=[]),
        patch.object(job.engine, "match", return_value=[unmatched_candidate]),
        patch(
            "app.services.reconciliation.order_recon_job.dispatch_resolution_agent",
            side_effect=_fake_dispatch,
        ),
    ):
        await job.run(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

    assert called["count"] == 0
