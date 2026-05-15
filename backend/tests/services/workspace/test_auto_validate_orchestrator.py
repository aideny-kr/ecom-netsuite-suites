"""Auto-validate orchestrator unit tests."""

from __future__ import annotations

import uuid

import pytest

from app.services.workspace.auto_validate_orchestrator import (
    LOOP_BUDGET,
    AutoValidateOrchestrator,
    LoopBudgetExceeded,
    get_orchestrator,
)


@pytest.mark.asyncio
async def test_debounce_cancels_superseded_run(monkeypatch) -> None:
    orchestrator = AutoValidateOrchestrator()
    workspace_id = uuid.uuid4()
    enqueued: list[uuid.UUID] = []

    async def fake_create_run(*, workspace_id: uuid.UUID, **_) -> uuid.UUID:
        run_id = uuid.uuid4()
        enqueued.append(run_id)
        return run_id

    orchestrator._create_run = fake_create_run

    first = await orchestrator.enqueue(
        workspace_id=workspace_id, changeset_id=uuid.uuid4(), tenant_id=uuid.uuid4(), triggered_by=uuid.uuid4()
    )
    second = await orchestrator.enqueue(
        workspace_id=workspace_id, changeset_id=uuid.uuid4(), tenant_id=uuid.uuid4(), triggered_by=uuid.uuid4()
    )

    assert orchestrator.is_cancelled(first) is True
    assert orchestrator.is_cancelled(second) is False


@pytest.mark.asyncio
async def test_loop_budget_blocks_after_n_auto_fixes() -> None:
    orchestrator = AutoValidateOrchestrator()
    changeset_id = uuid.uuid4()

    for _ in range(LOOP_BUDGET):
        orchestrator.record_auto_fix(changeset_id)

    assert orchestrator.under_budget(changeset_id) is False
    with pytest.raises(LoopBudgetExceeded):
        orchestrator.assert_under_budget(changeset_id)


def test_fingerprint_dedup_blocks_repeat_auto_propose() -> None:
    orchestrator = AutoValidateOrchestrator()
    changeset_id = uuid.uuid4()
    fp = "a" * 64

    assert orchestrator.should_auto_propose(changeset_id, fp) is True
    orchestrator.record_auto_propose(changeset_id, fp)
    assert orchestrator.should_auto_propose(changeset_id, fp) is False


def test_get_orchestrator_returns_singleton() -> None:
    """The module-level instance is reused across calls (per-process state)."""
    a = get_orchestrator()
    b = get_orchestrator()
    assert a is b
