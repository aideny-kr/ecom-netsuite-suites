# Bet 3 Rung 1 — Scheduled Recon Runs + Dry-Run Autonomy Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nightly scheduled reconciliation runs for opted-in tenants, plus a report-only "autonomy envelope" dry-run job that records (in audit, as a system actor) which lines the system WOULD auto-approve — with zero status mutations and zero NetSuite writes.

**Architecture:** Reuse the existing-but-never-wired `tasks.reconciliation_run` Celery task by registering it and dispatching it from a new Beat fan-out task, gated per-tenant by a new `recon_scheduled_runs` feature flag (default off). A new pure envelope evaluator (`autonomy_envelope.py`) implements the v1 criteria from the trust-model decision doc (D2: bucket `matches` + deterministic + zero variance + non-terminal status). A second Beat job, gated by a new `autonomous_recon` flag (default off), evaluates the envelope against each tenant's latest completed run and writes ONE `actor_type="system"` audit event with the report. No migrations, no new tables, no NetSuite writes anywhere.

**Tech Stack:** FastAPI/SQLAlchemy 2.0 async, Celery + Beat (`InstrumentedTask`), pytest (`backend/tests/conftest.py` fixtures: `db`, `tenant_a`, `create_test_recon_run`, `create_test_recon_result`).

**Decision doc:** `docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md` (D1=Rung 1, D2 envelope v1, D5 flags default-off).

**Tier: T2** (cron/Beat jobs + feature flags + recon domain). Pre-merge: blocking `code-review-multiangle` gate. No alembic migration in this slice (deliberate — avoids multi-head risk).

---

## Context for the implementer (read first)

- Repo root for this work: `/Users/aidenyi/projects/ecom-netsuite-suites/.claude/worktrees/grill-review-integration`. **Always `cd backend/` inside the worktree before running python/pytest** (the venv `.pth` resolves to the main checkout otherwise). Test command: `backend/.venv/bin/python -m pytest` from `backend/`.
- DB-backed tests need the local docker Postgres up (`docker compose up -d postgres` if not running) and may need sandbox-disabled bash.
- `tasks.reconciliation_run` already exists at `backend/app/workers/tasks/reconciliation_run.py` (wraps async `ReconJobRunner.run(date_from, date_to, ...)` → returns `ReconRunSummary.model_dump()`), but the module is **missing from `celery_app.conf.include`** — it is currently dead wiring. This plan registers it.
- Run statuses: `running` → `completed` | `failed` (set in `recon_job.py:68,111,143`); `close_period` later sets `closed`. Querying `status == "completed"` therefore naturally excludes running/failed/closed runs.
- Result statuses: approvable rows are anything NOT in `("approved", "rejected", "locked")` — mirrors the bulk-approve guard (`reconciliation.py:570-693`).
- Bucket constants: `from app.services.reconciliation.four_bucket_classifier import BUCKET_MATCHES` (`"matches"`).
- Audit: `audit_service.log_event(...)` is async, supports `actor_type="system"` + `actor_id=None` (`backend/app/services/audit_service.py:9`).
- RLS: sessions opened in workers must set tenant context before queries: `from app.core.database import set_tenant_context; await set_tenant_context(db, tenant_id)` (`database.py:55`).
- Celery test pattern to copy: `backend/tests/workers/test_drive_rag_sync.py` (task registration, queue, beat entry, include assertions).
- Lint before any commit: `cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests` (CI runs BOTH; format-check failures are real failures).

## File structure

| File | Action | Responsibility |
|---|---|---|
| `backend/app/services/feature_flag_service.py` | Modify | +2 DEFAULT_FLAGS keys; + `list_enabled_tenants()` helper |
| `backend/app/services/reconciliation/autonomy_envelope.py` | Create | PURE v1 envelope evaluator + report dataclass |
| `backend/app/workers/tasks/recon_scheduled_run_all.py` | Create | Beat fan-out: dispatch `tasks.reconciliation_run` per flag-enabled tenant |
| `backend/app/workers/tasks/recon_envelope_dry_run.py` | Create | Per-tenant dry-run task + Beat fan-out (report-only) |
| `backend/app/workers/celery_app.py` | Modify | include 3 modules (incl. dead `reconciliation_run`) + 2 beat entries |
| `backend/tests/test_autonomy_flags.py` | Create | flag defaults |
| `backend/tests/test_autonomy_envelope.py` | Create | pure evaluator unit tests |
| `backend/tests/workers/test_recon_scheduled_run_all.py` | Create | wiring + tenant-filter + dispatch tests |
| `backend/tests/workers/test_recon_envelope_dry_run.py` | Create | dry-run behavior (audit written, nothing mutated, flag-off skip) |

---

### Task 1: Feature flags (`recon_scheduled_runs`, `autonomous_recon`) + tenant-list helper

**Files:**
- Modify: `backend/app/services/feature_flag_service.py`
- Test: `backend/tests/test_autonomy_flags.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Bet 3 Rung 1 flags: scheduled recon runs + autonomy envelope are opt-in,
default OFF for every tenant (decision doc D5)."""

import uuid

from app.services import feature_flag_service


def test_rung1_flags_registered_and_default_off():
    assert feature_flag_service.DEFAULT_FLAGS["recon_scheduled_runs"] is False
    assert feature_flag_service.DEFAULT_FLAGS["autonomous_recon"] is False
    assert feature_flag_service.is_known_flag("recon_scheduled_runs")
    assert feature_flag_service.is_known_flag("autonomous_recon")


async def test_list_enabled_tenants_filters_by_flag_and_enabled(db, tenant_a, tenant_b):
    await feature_flag_service.set_flag(db, tenant_a.id, "recon_scheduled_runs", True)
    await feature_flag_service.set_flag(db, tenant_b.id, "recon_scheduled_runs", False)
    await feature_flag_service.set_flag(db, tenant_b.id, "autonomous_recon", True)

    enabled = await feature_flag_service.list_enabled_tenants(db, "recon_scheduled_runs")

    assert enabled == [tenant_a.id]
    assert all(isinstance(t, uuid.UUID) for t in enabled)
```

Note: `tenant_a`/`tenant_b` fixtures both exist in `backend/tests/conftest.py` (lines 397/402).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_autonomy_flags.py -v`
Expected: FAIL — `KeyError: 'recon_scheduled_runs'` and `AttributeError: ... has no attribute 'list_enabled_tenants'`

- [ ] **Step 3: Implement**

In `feature_flag_service.py`, extend `DEFAULT_FLAGS`:

```python
    "plan_mode_enabled": False,
    # Bet 3 Rung 1 (decision doc 2026-06-10): both default OFF for every tenant.
    "recon_scheduled_runs": False,   # nightly scheduled matching (read+match only)
    "autonomous_recon": False,       # autonomy envelope evaluation (dry-run in Rung 1)
```

Add after `get_all_flags`:

```python
async def list_enabled_tenants(db: AsyncSession, flag_key: str) -> list[uuid.UUID]:
    """All tenant_ids with flag_key explicitly enabled. No cache — used by Beat fan-outs."""
    result = await db.execute(
        select(TenantFeatureFlag.tenant_id).where(
            TenantFeatureFlag.flag_key == flag_key,
            TenantFeatureFlag.enabled.is_(True),
        )
    )
    return [row[0] for row in result.all()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_autonomy_flags.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Lint + commit**

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests
git add backend/app/services/feature_flag_service.py backend/tests/test_autonomy_flags.py
git commit -m "feat(recon): add recon_scheduled_runs + autonomous_recon flags (default off)"
```

---

### Task 2: Pure envelope evaluator

**Files:**
- Create: `backend/app/services/reconciliation/autonomy_envelope.py`
- Test: `backend/tests/test_autonomy_envelope.py`

- [ ] **Step 1: Write the failing tests**

```python
"""v1 autonomy envelope (Bet 3 Rung 1): ONLY deterministic, zero-variance,
bucket='matches', non-terminal rows qualify. Everything else is excluded with
a tallied reason. Pure function — these tests use unsaved ORM objects."""

import uuid
from decimal import Decimal

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.autonomy_envelope import ENVELOPE_VERSION, evaluate


def _result(**overrides) -> ReconciliationResult:
    defaults = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        match_type="deterministic",
        status="suggested",
        bucket="matches",
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("100.00"),
        currency="USD",
    )
    defaults.update(overrides)
    return ReconciliationResult(**defaults)


def test_qualifying_row_is_candidate():
    row = _result()
    report = evaluate([row])
    assert report.candidate_count == 1
    assert report.candidate_ids == (str(row.id),)
    assert report.candidate_total_amount == Decimal("100.00")
    assert report.excluded == {}
    assert report.envelope_version == ENVELOPE_VERSION


def test_exclusion_reasons_are_tallied():
    rows = [
        _result(status="approved"),                                   # terminal_status
        _result(status="locked"),                                     # terminal_status
        _result(bucket="needs_review"),                               # bucket_not_matches
        _result(match_type="fuzzy", bucket="rules"),                  # bucket_not_matches (bucket checked first)
        _result(variance_amount=Decimal("0.01"), bucket="matches"),   # has_variance
        _result(variance_amount=None, bucket="matches"),              # has_variance (None = unknown = out)
    ]
    report = evaluate(rows)
    assert report.candidate_count == 0
    assert report.excluded == {
        "terminal_status": 2,
        "bucket_not_matches": 2,
        "has_variance": 2,
    }


def test_non_deterministic_in_matches_bucket_is_excluded():
    # Defensive: bucket says 'matches' but match_type disagrees — match_type wins.
    report = evaluate([_result(match_type="fuzzy", bucket="matches")])
    assert report.candidate_count == 0
    assert report.excluded == {"not_deterministic": 1}


def test_total_amount_sums_candidates_only_and_payload_is_json_safe():
    rows = [_result(stripe_amount=Decimal("10.50")), _result(stripe_amount=Decimal("2.25")),
            _result(status="approved", stripe_amount=Decimal("999.99"))]
    report = evaluate(rows)
    assert report.candidate_total_amount == Decimal("12.75")
    payload = report.to_payload()
    assert payload["candidate_total_amount"] == "12.75"   # str, not Decimal
    assert payload["candidate_count"] == 2
    assert isinstance(payload["candidate_ids"], list)


def test_empty_input():
    report = evaluate([])
    assert report.candidate_count == 0
    assert report.candidate_ids == ()
    assert report.candidate_total_amount == Decimal("0")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_autonomy_envelope.py -v`
Expected: FAIL with `ModuleNotFoundError: ... autonomy_envelope`

- [ ] **Step 3: Implement**

```python
"""Rung-1 autonomy envelope (Bet 3): which recon lines a system actor may
auto-approve. v1 is deliberately the tightest possible envelope —
bucket 'matches' + deterministic match + zero variance + non-terminal status
(decision doc D2: docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md).

PURE module: no DB access, no side effects. The dry-run worker feeds it rows
and reports what WOULD be auto-approved; nothing here mutates state. NOT
confidence-gated — the advisory scorer is uncalibrated (0-approval corpus).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import BUCKET_MATCHES

ENVELOPE_VERSION = "v1"

# Mirrors the bulk-approve guard (reconciliation.py approve_bucket): rows
# already approved/rejected/locked can never be acted on again.
_TERMINAL_STATUSES = ("approved", "rejected", "locked")


@dataclass(frozen=True)
class EnvelopeReport:
    envelope_version: str
    candidate_ids: tuple[str, ...]
    candidate_count: int
    candidate_total_amount: Decimal
    excluded: dict[str, int]

    def to_payload(self) -> dict:
        """JSON-safe dict for audit payloads (Decimal → str)."""
        return {
            "envelope_version": self.envelope_version,
            "candidate_count": self.candidate_count,
            "candidate_total_amount": str(self.candidate_total_amount),
            "candidate_ids": list(self.candidate_ids),
            "excluded": dict(self.excluded),
        }


def evaluate(results: Iterable[ReconciliationResult]) -> EnvelopeReport:
    """Classify rows into envelope candidates vs excluded-with-reason."""
    candidates: list[ReconciliationResult] = []
    excluded: dict[str, int] = {}

    def _exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for row in results:
        if row.status in _TERMINAL_STATUSES:
            _exclude("terminal_status")
        elif row.bucket != BUCKET_MATCHES:
            _exclude("bucket_not_matches")
        elif row.match_type != "deterministic":
            _exclude("not_deterministic")
        elif row.variance_amount is None or row.variance_amount != Decimal("0"):
            _exclude("has_variance")
        else:
            candidates.append(row)

    total = sum((c.stripe_amount or Decimal("0") for c in candidates), Decimal("0"))
    return EnvelopeReport(
        envelope_version=ENVELOPE_VERSION,
        candidate_ids=tuple(str(c.id) for c in candidates),
        candidate_count=len(candidates),
        candidate_total_amount=total,
        excluded=excluded,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_autonomy_envelope.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests
git add backend/app/services/reconciliation/autonomy_envelope.py backend/tests/test_autonomy_envelope.py
git commit -m "feat(recon): v1 autonomy envelope evaluator (pure, report-only)"
```

---

### Task 3: Scheduled-run fan-out Beat task

**Files:**
- Create: `backend/app/workers/tasks/recon_scheduled_run_all.py`
- Test: `backend/tests/workers/test_recon_scheduled_run_all.py`

- [ ] **Step 1: Write the failing tests**

```python
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
        mod.celery_app, "send_task",
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
```

Design note driving the test: put the body in `async def collect_and_dispatch(db)` so tests inject the fixture session; the Celery task wrapper only opens the session and calls it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/workers/test_recon_scheduled_run_all.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Scheduled Celery task: nightly reconciliation runs for opted-in tenants.

Dispatches the existing ``tasks.reconciliation_run`` per tenant whose
``recon_scheduled_runs`` feature flag is enabled. Read+match only — results
land in reconciliation_runs/_results exactly like a user-triggered run; no
NetSuite writes, no approvals (Bet 3 Rung 1 groundwork).
"""

import logging
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

SCHEDULED_RUN_FLAG = "recon_scheduled_runs"
SCHEDULED_RUN_WINDOW_DAYS = 7


async def collect_and_dispatch(db: AsyncSession) -> dict:
    """Find flag-enabled tenants and enqueue one reconciliation run each."""
    from app.services import feature_flag_service

    tenant_ids = await feature_flag_service.list_enabled_tenants(db, SCHEDULED_RUN_FLAG)

    today = date.today()
    date_from = (today - timedelta(days=SCHEDULED_RUN_WINDOW_DAYS)).isoformat()
    date_to = today.isoformat()

    stats = {"dispatched": 0, "failed": 0}
    for tenant_id in tenant_ids:
        try:
            celery_app.send_task(
                "tasks.reconciliation_run",
                kwargs={"tenant_id": str(tenant_id), "date_from": date_from, "date_to": date_to},
                queue="recon",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("recon_scheduled_run_all.dispatch_failed", extra={"tenant_id": str(tenant_id)})
    logger.info("recon_scheduled_run_all.completed", extra=stats)
    return stats


@celery_app.task(base=InstrumentedTask, name="tasks.recon_scheduled_run_all", queue="recon")
def recon_scheduled_run_all():
    """Beat entry point. Opens its own session; logic lives in collect_and_dispatch()."""
    import asyncio

    from app.core.database import async_session_factory

    async def _run() -> dict:
        async with async_session_factory() as db:
            return await collect_and_dispatch(db)

    return asyncio.run(_run())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/workers/test_recon_scheduled_run_all.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests
git add backend/app/workers/tasks/recon_scheduled_run_all.py backend/tests/workers/test_recon_scheduled_run_all.py
git commit -m "feat(recon): nightly scheduled-run fan-out task (flag-gated, read+match only)"
```

---

### Task 4: Envelope dry-run task (report-only)

**Files:**
- Create: `backend/app/workers/tasks/recon_envelope_dry_run.py`
- Test: `backend/tests/workers/test_recon_envelope_dry_run.py`

- [ ] **Step 1: Write the failing tests**

```python
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
        (await db.execute(select(AuditEvent).where(
            AuditEvent.action == DRY_RUN_ACTION,
            AuditEvent.resource_id == str(run_id),
        ))).scalars().all()
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
        db, tenant_a.id, run.id, status="suggested",
        match_type="fuzzy", variance_amount=Decimal("5.00"), variance_type="fees",
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
```

Note: check `create_test_recon_run`'s signature for the `status=` kwarg (conftest line ~314). If it doesn't accept `status`, set `run.status = "completed"` after creation instead — do NOT change the shared factory.
Note: `feature_flag_service.clear_cache()` is required because the module-level TTL cache survives across tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/workers/test_recon_envelope_dry_run.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Rung-1 dry-run job (Bet 3): report-only autonomy-envelope evaluation.

For each tenant with ``autonomous_recon`` enabled, evaluates the v1 envelope
against the latest COMPLETED reconciliation run and writes ONE audit event
(actor_type="system") with the report. NEVER mutates result rows; NEVER
writes to NetSuite. This builds the evidence base for enforcement later.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

AUTONOMY_FLAG = "autonomous_recon"
DRY_RUN_ACTION = "recon.envelope.dry_run"


async def dry_run_for_tenant(db: AsyncSession, tenant_id: str) -> dict:
    """Evaluate the envelope for one tenant. Report-only: one audit event, no mutations."""
    from app.models.reconciliation import ReconciliationResult, ReconciliationRun
    from app.services import audit_service, feature_flag_service
    from app.services.reconciliation import autonomy_envelope

    tid = uuid.UUID(tenant_id)
    if not await feature_flag_service.is_enabled(db, tid, AUTONOMY_FLAG):
        return {"tenant_id": tenant_id, "skipped": "flag_disabled"}

    run = (
        await db.execute(
            select(ReconciliationRun)
            .where(
                ReconciliationRun.tenant_id == tid,
                ReconciliationRun.status == "completed",
            )
            .order_by(ReconciliationRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return {"tenant_id": tenant_id, "skipped": "no_completed_run"}

    results = (
        (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)))
        .scalars()
        .all()
    )
    report = autonomy_envelope.evaluate(results)

    await audit_service.log_event(
        db=db,
        tenant_id=tid,
        category="reconciliation",
        action=DRY_RUN_ACTION,
        actor_id=None,
        actor_type="system",
        resource_type="reconciliation_run",
        resource_id=str(run.id),
        correlation_id=f"envelope-dryrun-{uuid.uuid4().hex}",
        payload=report.to_payload(),
    )
    await db.commit()
    return {"tenant_id": tenant_id, "run_id": str(run.id), **report.to_payload()}


@celery_app.task(base=InstrumentedTask, name="tasks.recon_envelope_dry_run", queue="recon")
def recon_envelope_dry_run(tenant_id: str, **kwargs):
    """Per-tenant dry run. Opens its own RLS-scoped session."""
    import asyncio

    from app.core.database import async_session_factory, set_tenant_context

    async def _run() -> dict:
        async with async_session_factory() as db:
            await set_tenant_context(db, tenant_id)
            return await dry_run_for_tenant(db, tenant_id)

    return asyncio.run(_run())


@celery_app.task(base=InstrumentedTask, name="tasks.recon_envelope_dry_run_all", queue="recon")
def recon_envelope_dry_run_all():
    """Beat fan-out: one dry-run task per autonomous_recon-enabled tenant."""
    import asyncio

    from app.core.database import async_session_factory
    from app.services import feature_flag_service

    async def _tenants() -> list[str]:
        async with async_session_factory() as db:
            return [str(t) for t in await feature_flag_service.list_enabled_tenants(db, AUTONOMY_FLAG)]

    stats = {"dispatched": 0, "failed": 0}
    for tenant_id in asyncio.run(_tenants()):
        try:
            celery_app.send_task(
                "tasks.recon_envelope_dry_run",
                kwargs={"tenant_id": tenant_id},
                queue="recon",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("recon_envelope_dry_run_all.dispatch_failed", extra={"tenant_id": tenant_id})
    logger.info("recon_envelope_dry_run_all.completed", extra=stats)
    return stats
```

RLS note: `set_tenant_context` uses `SET LOCAL`, which only persists inside a transaction — the async session begins one implicitly on first statement, but verify the `is_enabled` query runs in the same transaction (it does unless something commits in between; `dry_run_for_tenant` commits only at the end). If tests against the RLS-enforced test DB fail on visibility, call `await set_tenant_context(db, tenant_id)` again after the final `commit()`-preceding queries — but do NOT restructure beyond that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/workers/test_recon_envelope_dry_run.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests
git add backend/app/workers/tasks/recon_envelope_dry_run.py backend/tests/workers/test_recon_envelope_dry_run.py
git commit -m "feat(recon): autonomy-envelope dry-run task (report-only, system-actor audit)"
```

---

### Task 5: Celery wiring — include modules (incl. dead `reconciliation_run`) + Beat schedule

**Files:**
- Modify: `backend/app/workers/celery_app.py` (include list ~line 29-53; beat_schedule ~line 55-115)
- Test: `backend/tests/workers/test_recon_scheduled_run_all.py` (extend), `backend/tests/workers/test_recon_envelope_dry_run.py` (extend)

- [ ] **Step 1: Write the failing tests** (append to the two existing worker test files)

In `test_recon_scheduled_run_all.py`:

```python
def test_include_and_beat_wiring():
    from app.workers.celery_app import celery_app

    # tasks.reconciliation_run was previously DEAD (defined but unregistered) —
    # the fan-out dispatches it by name, so it MUST be in include.
    assert "app.workers.tasks.reconciliation_run" in celery_app.conf.include
    assert "app.workers.tasks.recon_scheduled_run_all" in celery_app.conf.include

    entry = celery_app.conf.beat_schedule["recon-scheduled-run-nightly"]
    assert entry["task"] == "tasks.recon_scheduled_run_all"
```

In `test_recon_envelope_dry_run.py`:

```python
def test_include_and_beat_wiring():
    from app.workers.celery_app import celery_app

    assert "app.workers.tasks.recon_envelope_dry_run" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule["recon-envelope-dry-run-nightly"]
    assert entry["task"] == "tasks.recon_envelope_dry_run_all"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/workers/test_recon_scheduled_run_all.py::test_include_and_beat_wiring tests/workers/test_recon_envelope_dry_run.py::test_include_and_beat_wiring -v`
Expected: FAIL (AssertionError / KeyError)

- [ ] **Step 3: Implement**

In `celery_app.conf.include`, after `"app.workers.tasks.netsuite_deposit_sync_all",` add:

```python
    "app.workers.tasks.reconciliation_run",
    "app.workers.tasks.recon_scheduled_run_all",
    "app.workers.tasks.recon_envelope_dry_run",
```

In `beat_schedule`, after the `"netsuite-deposit-sync-nightly"` entry add:

```python
    # Bet 3 Rung 1 — both flag-gated per tenant (default off → no-op fan-outs).
    # 03:30 UTC: after deposit sync (02:00) has landed the night's data.
    "recon-scheduled-run-nightly": {
        "task": "tasks.recon_scheduled_run_all",
        "schedule": crontab(hour=3, minute=30),
    },
    # 04:30 UTC: after scheduled runs complete; report-only envelope evaluation.
    "recon-envelope-dry-run-nightly": {
        "task": "tasks.recon_envelope_dry_run_all",
        "schedule": crontab(hour=4, minute=30),
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/workers/ tests/test_autonomy_flags.py tests/test_autonomy_envelope.py -v`
Expected: PASS (all)

- [ ] **Step 5: Lint + commit**

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests
git add backend/app/workers/celery_app.py backend/tests/workers/test_recon_scheduled_run_all.py backend/tests/workers/test_recon_envelope_dry_run.py
git commit -m "feat(recon): register reconciliation_run + wire Rung-1 beat schedule"
```

---

### Task 6: Full regression + recon e2e backbone

- [ ] **Step 1: Run the full backend suite**

Run: `cd backend && .venv/bin/python -m pytest`
Expected: PASS, zero regressions (some suites need the docker Postgres up).

- [ ] **Step 2: Run the recon seeded-tenant e2e backbone (T2 requirement)**

Run: `cd backend && .venv/bin/python -m pytest tests/e2e/test_recon_lifecycle_e2e.py -v`
Expected: PASS (this slice must not perturb the recon write-path regression backbone).

- [ ] **Step 3: Final lint sweep + commit anything outstanding**

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m ruff format --check app tests
git status   # should be clean except untracked plan/spec docs
```

---

## Out of scope (explicitly)

- Any status mutation by the system actor (that's Rung 1 *enforcement* — a later slice, after dry-run mileage).
- Any NetSuite write (Rung 2+, blocked on D3/D4).
- Dollar-cap TenantConfig columns (needs migration; deferred until enforcement slice so this slice stays migration-free).
- Frontend surfacing of dry-run reports (later; reports are queryable via audit events now).
- Confidence-gating (scorer uncalibrated — decision doc D2).

## Post-merge (not part of this plan's tasks)

- T2 blocking gate: `Workflow({name:"code-review-multiangle", args:{target:"<PR#>"}})` — check `status` + `codex_used`.
- Staging: every main merge auto-deploys; watch worker+beat containers restart. Verify with `docker exec ecom-netsuite-beat-1 celery -A app.workers.celery_app inspect registered` that the three new task names appear.
- Enable `recon_scheduled_runs` + `autonomous_recon` for the `uat-smoke` tenant only; trigger `tasks.recon_envelope_dry_run` manually and verify the audit event lands with `actor_type="system"` and zero status changes.
