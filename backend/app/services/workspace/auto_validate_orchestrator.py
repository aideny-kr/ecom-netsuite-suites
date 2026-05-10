"""Auto-validate orchestrator: debounce, loop budget, fingerprint dedup.

Three responsibilities, one class, no I/O:
1. Debounce: when a workspace gets multiple apply_patch events in quick succession,
   only the most recent enqueued validate run actually executes; superseded ones
   are marked cancelled (the runner skips cancelled runs at execute_run entry).
2. Loop budget: per changeset, the agent gets at most LOOP_BUDGET auto-fix
   rounds before further auto-propose attempts are refused (the agent must
   narrate-only beyond that).
3. Fingerprint dedup: the same finding fingerprint cannot trigger an auto-propose
   twice within the same changeset.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Awaitable, Callable

LOOP_BUDGET = 3
DEBOUNCE_SECONDS = 2.0


class LoopBudgetExceeded(Exception):  # noqa: N818 — domain term, not a generic Error
    """Raised when assert_under_budget is called past LOOP_BUDGET auto-fixes."""


class AutoValidateOrchestrator:
    """Per-process state. Single instance reused across the FastAPI app."""

    def __init__(self) -> None:
        self._latest_run_per_workspace: dict[uuid.UUID, uuid.UUID] = {}
        self._cancelled: set[uuid.UUID] = set()
        self._auto_fix_count: dict[uuid.UUID, int] = defaultdict(int)
        self._proposed_fingerprints: dict[uuid.UUID, set[str]] = defaultdict(set)
        # Set by the FastAPI lifespan to the runner_service.create_run coroutine.
        self._create_run: Callable[..., Awaitable[uuid.UUID]] | None = None

    async def enqueue(
        self,
        *,
        workspace_id: uuid.UUID,
        changeset_id: uuid.UUID,
        tenant_id: uuid.UUID,
        triggered_by: uuid.UUID,
    ) -> uuid.UUID:
        """Enqueue a validate run; cancel any in-flight queued run for the same workspace."""
        if self._create_run is None:
            raise RuntimeError("AutoValidateOrchestrator not initialized: _create_run is None")

        previous = self._latest_run_per_workspace.get(workspace_id)
        if previous is not None:
            self._cancelled.add(previous)

        run_id = await self._create_run(
            workspace_id=workspace_id,
            changeset_id=changeset_id,
            tenant_id=tenant_id,
            triggered_by=triggered_by,
            run_type="suitecloud_validate",
        )
        self._latest_run_per_workspace[workspace_id] = run_id
        return run_id

    def is_cancelled(self, run_id: uuid.UUID) -> bool:
        return run_id in self._cancelled

    def under_budget(self, changeset_id: uuid.UUID) -> bool:
        return self._auto_fix_count[changeset_id] < LOOP_BUDGET

    def assert_under_budget(self, changeset_id: uuid.UUID) -> None:
        if not self.under_budget(changeset_id):
            raise LoopBudgetExceeded(f"changeset {changeset_id} exceeded {LOOP_BUDGET} auto-fix rounds")

    def record_auto_fix(self, changeset_id: uuid.UUID) -> None:
        self._auto_fix_count[changeset_id] += 1

    def should_auto_propose(self, changeset_id: uuid.UUID, fingerprint: str) -> bool:
        return fingerprint not in self._proposed_fingerprints[changeset_id]

    def record_auto_propose(self, changeset_id: uuid.UUID, fingerprint: str) -> None:
        self._proposed_fingerprints[changeset_id].add(fingerprint)


_INSTANCE: AutoValidateOrchestrator | None = None


def get_orchestrator() -> AutoValidateOrchestrator:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoValidateOrchestrator()
    return _INSTANCE
