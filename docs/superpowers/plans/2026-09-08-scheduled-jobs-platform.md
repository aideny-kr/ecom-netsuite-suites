# Scheduled Jobs platform (Slice 2) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing per-tenant `schedules` table into a working Scheduled Jobs system: a plain-language instruction compiled into an allow-listed plan, approved by a person, run deterministically by a Beat sweep with budgets and reasons, managed on a page that reproduces the approved mock, with Inventory Aging Weekly as the first job.

**Architecture:** Registry (allow-list of step types with executors) → compiler (one structured-output LLM call validated against the registry) → executor (Beat sweep, `jobs` rows, reason enum, idempotent writes) → API (schedules routes) → page (list, job, new job). Report delivery steps call Slice 1's services.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Celery Beat + `InstrumentedTask`, `croniter`, the chat LLM adapter with structured output, Next 14 / React 18 / TanStack Query / vitest.

**Spec:** `docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md` Part B (binding). Mock: `/Users/aidenyi/.claude/jobs/bc60c23d/tmp/scheduled-jobs-mock.html` — reproduce its sections, labels, and copy.

## Global Constraints

- TDD; red runs in reports. The registry is the only place a step type exists; the compiler's schema is generated from it; unknown types are rejected at compile AND at run.
- The agent runs only at compile time. Runs replay `plan_json`; no LLM call inside the executor.
- Every run: one `jobs` row via the instrumented task base, `result_summary.reason ∈ {done, budget, stall, error, blocked}`, budget enforced between steps, WRITE steps audit `started` with the idempotency key before the call. Missed runs catch up once. Failure → one retry after 15 min → pause + `pause_reason` + owner notification.
- NetSuite and Celigo writes are not registry steps. The recon step never approves/locks/posts.
- Multi-tenant: every query tenant-scoped; the sweep uses `FOR UPDATE SKIP LOCKED`; `set_tenant_context` where the repo requires it.
- Frontend: `apiClient` only; `"use client"`; `queryState()`; icons from `lucide-react`; the page copy matches the mock verbatim where the mock has copy.
- Commands: backend as in Slice 1 (`…/.venv/bin/python -m pytest` from `backend/` against the local verify DB; alembic to the local DB only); frontend `cd frontend && npx vitest run src/app/\(dashboard\)/scheduled-jobs src/components/scheduled-jobs src/hooks && npx tsc --noEmit -p tsconfig.json && npx eslint <files>`.
- Never amend; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: Schema + registry

**Files:**
- Create: `backend/alembic/versions/100_scheduled_jobs.py`, `backend/app/services/jobs/__init__.py`, `backend/app/services/jobs/registry.py`
- Modify: `backend/app/models/pipeline.py` (`Schedule` columns per spec §B1)
- Test: `backend/tests/jobs/test_registry.py`, `backend/tests/test_scheduled_jobs_migration.py` (pattern: `tests/test_report_migration.py`)
- Read first: `models/pipeline.py`, `services/schedule_service.py`, `schemas/schedule.py`, the latest migration on the branch, Slice 1's `report_delivery.py` / `report_pdf.py` / `report_excel.py` / `compose_playbook_report` signatures, `workers/tasks/recon_scheduled_run*.py`, the `bigquery_sql` tool executor.

**Interfaces:**
- Produces: `StepSpec`, `STEP_REGISTRY` with v1 types `bigquery_sql`, `report.compose`, `report.render_pdf`, `report.build_xlsx`, `drive.upload`, `recon.run`; `validate_plan(plan: dict) -> ValidatedPlan` (raises `PlanInvalid` with a per-step message); `plan_schema() -> dict` (JSON schema for the compiler); `StepContext` (run id, tenant, budget, artifacts dict passed between steps).

**Steps:**
- [ ] Failing tests: registry has exactly the v1 types with the right kinds; `validate_plan` rejects an unknown type, a `drive.upload` referencing an artifact no earlier step produces, and params violating a step's schema; `plan_schema()` enumerates only registry types; migration up/down round-trips and the new columns exist with defaults.
- [ ] Implement; commit: `feat(jobs): scheduled job schema + step registry (allow-list)`.

---

### Task 2: Compiler

**Files:**
- Create: `backend/app/services/jobs/compiler.py`
- Test: `backend/tests/jobs/test_compiler.py`
- Read first: how the chat adapter is called with structured output today (grep `structured_output`, `tool_choice`, the LLM adapter in `services/chat/`), BYOK routing, the audit service.

**Interfaces:**
- Produces: `compile_instruction(db, *, tenant_id, instruction, actor_id, llm=None) -> CompiledPlan | Clarification`, `plan_diff(old: dict, new: dict) -> list[DiffLine(kind: "add"|"del"|"ctx", step: int|None, text)]`, `CompiledPlan(plan_json, summary_line, kinds: set[str], model)`.

**Steps:**
- [ ] Failing tests with a fake LLM: a plan echoing the mock's instruction compiles into the five steps in order; an invalid step type in the model output triggers one repair round then `Clarification`; a missing required param yields `Clarification(question)`; the diff for "add Virtual" produces the mock's three hunks; an audit event per compile; the tenant's available locations are fed to the prompt (source: the snapshot's distinct locations via a registry-provided context hook).
- [ ] Implement; commit: `feat(jobs): compile an instruction into an allow-listed plan with clarification and diff`.

---

### Task 3: Executor + Beat sweep

**Files:**
- Create: `backend/app/workers/tasks/scheduled_jobs.py`
- Modify: `backend/app/workers/celery_app.py` (Beat entry `scheduled-jobs-sweep`, every minute, gated by a `SCHEDULED_JOBS_ENABLED` setting default true), `backend/app/services/schedule_service.py` (due computation helpers)
- Test: `backend/tests/jobs/test_executor.py`, `backend/tests/e2e/test_scheduled_jobs_e2e.py` (seeded tenant)
- Read first: `workers/tasks/report_auto_refresh.py` (fan-out pattern, failure ladder), `workers/base_task.py`, `models/job.py`, `croniter` availability (add to pyproject if missing).

**Interfaces:**
- Produces: `scheduled_jobs_sweep_all` (Beat), `run_due_jobs(tenant_id)`, `run_schedule_now(schedule_id, *, use_pending: bool, actor_id)`, `RunOutcome(reason, jobs_row_id, outputs)`; `compute_next_run(cron, tz, after) -> datetime`.

**Steps:**
- [ ] Failing tests: due detection in the schedule's tz (a Monday 06:00 PT job is due at 13:00 UTC in September); `FOR UPDATE SKIP LOCKED` prevents a double run (two concurrent sweeps → one `jobs` row); catch-up runs once after a missed window and `next_run_at` advances past now; a step error → `reason=error`, a retry `jobs` row 15 min later, then `paused_at` + `pause_reason` and an owner-notification audit event; budget exceeded between steps → `reason=budget`; `drive.upload` audits `started` with `job:{id}:period:{key}` before the fake client is called; `use_pending=True` records the pending plan version on the run; e2e: a due job runs exactly once end to end with the fake step executors.
- [ ] Implement; commit: `feat(jobs): Beat sweep runs due scheduled jobs with budgets, reasons, retry-then-pause`.

---

### Task 4: API + MCP

**Files:**
- Modify: `backend/app/api/v1/schedules.py`, `backend/app/schemas/schedule.py`, `backend/app/services/schedule_service.py`, `backend/app/mcp/tools/schedule_ops.py` (create → compile; run implemented)
- Test: `backend/tests/api/test_schedules_api.py` (extend), `backend/tests/test_schedule_ops_tool.py`
- Read first: the existing routes, `entitlement_service` quota gate, permission helpers, `test_prompt_tool_sync.py` (tool inventory unchanged: same tool names, new behaviour).

**Steps:**
- [ ] Failing tests: every route in spec §B5 with gates and quota; `POST /schedules` returns 201 + `pending_approval` plan, or 409 with the clarification; `PATCH` with a new instruction fills `pending_plan_json` and a diff; approve bumps `plan_version`; run enqueues (fake celery) and returns the `jobs` id; pause/resume; runs list from `jobs`; MCP `execute_run` no longer returns the stub; `test_prompt_tool_sync` still green.
- [ ] Implement; commit: `feat(jobs): schedules API — compile, approve, run, pause, runs; MCP run implemented`.

---

### Task 5: Frontend — list page + nav

**Files:**
- Create: `frontend/src/app/(dashboard)/scheduled-jobs/page.tsx`, `frontend/src/components/scheduled-jobs/jobs-list.tsx`, `frontend/src/components/scheduled-jobs/shared.tsx` (tags, pills, tiles), `frontend/src/hooks/use-scheduled-jobs.ts`, tests under `frontend/src/components/scheduled-jobs/__tests__/`
- Modify: `frontend/src/lib/constants.ts` (`NAV_ITEMS`: Scheduled jobs, `/scheduled-jobs`, icon `CalendarClock`, between Reports and Settings)
- Read first: the mock's state one, `components/settings/jobs-section.tsx` + `hooks/use-jobs.ts` (system rows), `sidebar.tsx`, `lib/query-state.ts`, the frontend rule.

**Steps:**
- [ ] Failing tests: nav item present; tiles (Jobs with quota, Last 7 days, Needs attention, Next run) from the list response; table columns and cells as the mock (READ/WRITE tags, cron sub-line, last-run pill with when/took, next, delivers to, Run now/Resume/Open); system rows with the `system` pill and read-only actions; footer hint copy verbatim; pending never renders 0; error → notice; empty-state copy verbatim.
- [ ] Implement; commit: `feat(jobs): Scheduled jobs page — list, tiles, system rows, nav`.

---

### Task 6: Frontend — job detail

**Files:**
- Create: `frontend/src/app/(dashboard)/scheduled-jobs/[id]/page.tsx`, `frontend/src/components/scheduled-jobs/job-detail.tsx`, `instruction-panel.tsx`, `plan-panel.tsx`, `pending-change-panel.tsx`, `schedule-panel.tsx`, `delivery-panel.tsx`, `runs-panel.tsx`, tests
- Read first: the mock's state two (every label and the two notes verbatim), the diff line shape from the API.

**Steps:**
- [ ] Failing tests: head actions; instruction panel with Edit (textarea → PATCH) and "Ask the agent to adjust…" (opens the editor with the last clarification, v1); plan steps numbered with title/description/guard/tag/`allow-listed`; the "What the agent may not do here" note verbatim; pending-change panel only when present, with diff lines coloured add/del and the three buttons wired (approve → POST approve; run once → POST run `use_pending`; discard → PATCH clear); schedule panel segmented control + weekday/time/tz with cron mode and next-run text; delivery panel; runs table with reason text and outputs; Pause/Resume/Delete confirm.
- [ ] Implement; commit: `feat(jobs): Scheduled job detail — instruction, plan, pending change, schedule, delivery, runs`.

---

### Task 7: Frontend — new job flow + chat hand-off

**Files:**
- Create: `frontend/src/app/(dashboard)/scheduled-jobs/new/page.tsx`, `frontend/src/components/scheduled-jobs/new-job.tsx`, tests
- Modify: the chat's handling of the `schedule_ops` create result (a card/link "Review the plan on Scheduled jobs →") — find where tool results render cards (`frontend/src/components/chat/**`)
- Read first: the mock's state three, the chat card patterns.

**Steps:**
- [ ] Failing tests: step 1 textarea + hint copy + Compile plan; step 2 renders the plan or the clarification question with an answer box that re-compiles; schedule + delivery form; Save → list with the new row `pending_approval`; the chat card links to the job.
- [ ] Implement; commit: `feat(jobs): new job flow (instruction → plan → schedule) + chat hand-off`.

---

### Task 8: First job on Framework + live proof

**Files:**
- Create: `backend/scripts/seed_inventory_aging_job.py` (creates the schedule from the mock's instruction, compiles with the real compiler, leaves it `pending_approval`)
- Test: DB-backed test with the fake LLM.

**Steps:**
- [ ] Run on staging after deploy: approve on the page, Run now, confirm the run row (`done`, duration, outputs), the new report version, both Drive files; set the schedule Monday 06:00 America/Los_Angeles; record the first Monday run's outcome.
- [ ] Commit: `chore(jobs): seed the Inventory Aging Weekly job`.

---

## Verification

`scripts/verify.sh --full`; seeded-tenant e2e for the sweep; frontend suites + tsc + eslint; the rendered pages viewed against the mock (list, job, new job); blocking multi-angle gate on the backend (executor, compiler, API, migration); live run on staging before the first Monday.
