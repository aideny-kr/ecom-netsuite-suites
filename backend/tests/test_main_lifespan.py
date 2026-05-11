"""Lifespan wiring tests — focused on _wire_auto_validate_orchestrator.

The orchestrator is a process-singleton with state that must persist across
requests; the lifespan is the only place that wires the runner-aware closure.
This test confirms the closure dispatches the Celery task — without it,
auto-validate runs sit in `queued` forever (Codex P2 / Claude P0 finding).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.main import _wire_auto_validate_orchestrator
from app.services.workspace.auto_validate_orchestrator import get_orchestrator


@pytest.mark.asyncio
async def test_wired_create_run_dispatches_celery_task() -> None:
    """Lifespan-wired _create_run closure MUST call workspace_run_task.delay.

    Without the dispatch, runner_service.create_run only inserts a queued
    WorkspaceRun row; nothing polls the table, so the run sits forever.
    """
    workspace_id = uuid.uuid4()
    changeset_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run_id = uuid.uuid4()
    correlation_id = "test-correlation-id"

    fake_run = MagicMock()
    fake_run.id = run_id
    fake_run.tenant_id = tenant_id
    fake_run.correlation_id = correlation_id

    fake_session = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None

    fake_factory = MagicMock(return_value=fake_session)
    fake_create_run = AsyncMock(return_value=fake_run)
    fake_delay = MagicMock()

    with (
        patch("app.core.database.async_session_factory", new=fake_factory),
        patch("app.services.runner_service.create_run", new=fake_create_run),
        patch(
            "app.workers.tasks.workspace_run.workspace_run_task",
            new=MagicMock(delay=fake_delay),
        ),
    ):
        _wire_auto_validate_orchestrator()
        closure = get_orchestrator()._create_run
        assert closure is not None

        returned = await closure(
            workspace_id=workspace_id,
            changeset_id=changeset_id,
            tenant_id=tenant_id,
            triggered_by=user_id,
            run_type="suitecloud_validate",
        )

    assert returned == run_id
    fake_create_run.assert_awaited_once()
    fake_session.commit.assert_awaited_once()
    fake_delay.assert_called_once_with(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        correlation_id=correlation_id,
    )
