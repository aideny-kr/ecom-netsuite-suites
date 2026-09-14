# Celigo Flow Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Celigo's three levels — integrations tiles → an integration's flows table → every flow as a full page (navigator · canvas · inspector · script drawer) — inside the dev workspace, dense with synced facts, including the step-names sync, as one T2-gated PR.

**Architecture:** Backend stays read-only behind the `celigo` feature flag: one migration adds `celigo_flow_steps.reference_name`, the existing sync Phase D backfills it, and the three existing list/detail routes grow aggregate fields so each screen is one request; two small new routes serve grouped errors and config changes; a pure `topology.py` projects routers/branches out of the synced flow object. Frontend replaces `celigo-flow-map.tsx` with a `components/celigo/` module: a single URL-state hook, three pages, a deterministic layered layout (pure TS, DOM + SVG canvas, no reactflow), an inspector and a script drawer. The workspace page keeps swapping its whole Files panel group for the Celigo surface, so no deploy affordance can be in the tree.

**Tech Stack:** FastAPI + SQLAlchemy 2 async + Alembic + Pydantic v2 (`JsonValue`); Next.js 14 app router, React 18, TanStack Query v5, react-resizable-panels v4 (`Group`/`Panel`/`Separator`, `orientation`), Radix Dialog, cmdk 1.1, react-syntax-highlighter, Tailwind; pytest (real local Postgres), vitest + testing-library, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-02-celigo-flow-pages.md` (design artifact: `docs/superpowers/mockups/2026-09-02-celigo-flow-page.html`)

## Global Constraints

- Read-only surface: no route or UI that runs, enables, retries, resolves, syncs or edits anything. No "Sync now". "Open in Celigo ↗" is the only way out.
- Production only: every new query joins/filters through `celigo_integration_is_production()` / `celigo_script_is_production()` from `app/models/celigo.py`; no hand-written `sandbox` predicates.
- N2: script `content` is rendered only inside the script drawer, as inert text (`react-syntax-highlighter`, never `dangerouslySetInnerHTML`), never in the inspector, palette, tooltips, or any `window.claude` path. The banner copy is exactly: "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant."
- `celigo_flow_errors` rows are never deleted (audit trail; the AST test `test_no_celigo_code_path_deletes_flow_error_rows` enforces it).
- Every route in `backend/app/api/v1/celigo_flows.py` keeps the three dependencies: `require_permission("connections.view")`, `require_feature("celigo")`, `get_db`, and tenant-scopes every table it touches.
- Predicates are defined once in `app/models/celigo.py` (pattern: `celigo_error_is_open()`); response models name every field by hand (no `from_attributes`, no `raw_json` leakage).
- Migration `097_celigo_flow_step_reference_name` has `down_revision = "094_dashboard_preference_series"` (the current single head; filename numbers are not the chain). Apply it to the LOCAL docker Postgres only, never Supabase.
- Backend tests: `cd <worktree>/backend && /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest <path> -q` (worktree cwd first so `import app` resolves to the worktree; `DATABASE_URL` defaults to `postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite`; `docker compose up -d postgres` must be running). Lint: `ruff check` AND `ruff format --check`.
- Frontend tests: `cd <worktree>/frontend && npx vitest run <path>`; lint `npx next lint`; types `npx tsc --noEmit`. `"use client"` on any file using hooks; `apiClient` only; icons from `lucide-react`; query keys `["celigo", ...]`.
- Stall heuristic: only the 6-field cron shape `? <minutes> <hours> ? * *` yields an interval; stalled? = on, has interval, `last_synced_at - last_executed_at > 2 × interval`; the label always carries the question mark; paused flows are never stalled; no schedule ⇒ no stall claim; computed against the sync time, never the wall clock.
- Commit after every green task with a conventional message; never amend; one logical change per commit. Branch: `feat/celigo-flow-pages` in worktree `/Users/aidenyi/projects/ecom-netsuite-suites/.claude/worktrees/celigo-prod-flowmap`.
- Copy: bubble fallback titles are `add salesorder` (NetSuite destination), `lookup customer · search 5090` (NetSuite lookup), `HTTP export · name not synced` / `HTTP lookup · name not synced` (others). Never an invented name. Paused banner: "This flow is Off in Celigo — mirrored here, not changeable here." Unknown id: "This flow is not in the last sync." Quiet errors: "No open errors. Celigo reported 0 on the last sync, {relative time}."

---

## File structure

**Backend (modify unless marked new)**
- `backend/alembic/versions/097_celigo_flow_step_reference_name.py` — new; one nullable text column.
- `backend/app/models/celigo.py` — `CeligoFlowStep.reference_name`; new predicate `celigo_flow_is_on_demand()`.
- `backend/app/services/celigo/repository.py` — `backfill_flow_step_reference_info(... reference_name=...)`.
- `backend/app/services/celigo/sync_service.py` — `_process_reference_object` passes `name`.
- `backend/app/services/celigo/topology.py` — new, pure: `step_kind`, `count_rules`, `project_routers`, `script_family_facts`.
- `backend/app/core/exception_handlers.py` — new: `register_exception_handlers(app)`; `backend/app/main.py` calls it.
- `backend/app/api/v1/celigo_flows.py` — response models + the three grown routes + `GET /celigo/flows/{id}/errors`, `GET /celigo/integrations/{id}/changes`, `GET /celigo/flows/{id}/changes`.
- Tests: `backend/tests/test_celigo_repository.py`, `backend/tests/test_celigo_sync.py`, `backend/tests/test_celigo_topology.py` (new), `backend/tests/test_exception_handlers.py` (new), `backend/tests/api/test_celigo_flows_api.py`.

**Frontend (new directory `frontend/src/components/celigo/` unless marked)**
- `schedule.ts` — cron parsing, humanising, stall heuristic (pure).
- `shared.tsx` — `ErrorNotice`, `formatRelativeTime`, pills, medallions, `adaptorFamily`, `fallbackStepTitle`, `deriveFlowSummary`.
- `chips.ts` — `affordanceChips(step)` (pure).
- `layout.ts` — deterministic layered layout (pure).
- `celigo-route.ts` — `useCeligoRoute()` — the single URL writer.
- `celigo-surface.tsx` — root switch + `CeligoBreadcrumb`.
- `celigo-integrations-page.tsx`, `celigo-command-palette.tsx`, `celigo-integration-page.tsx`, `celigo-flow-page.tsx`, `celigo-flow-header.tsx`, `celigo-flow-navigator.tsx`, `celigo-flow-canvas.tsx`, `step-bubble.tsx`, `router-node.tsx`, `celigo-step-inspector.tsx`, `inspector-panels.tsx`, `celigo-script-drawer.tsx`.
- `frontend/src/hooks/use-celigo-flows.ts` — types grown; `useCeligoFlowErrors`, `useCeligoIntegrationChanges`, `useCeligoFlowChanges`.
- `frontend/src/app/(dashboard)/workspace/page.tsx` — surface from the URL; Celigo branch mounts `<CeligoSurface/>`; keyboard handler branches on surface.
- `frontend/src/components/settings/celigo-flow-map.tsx` + its test — deleted in Task 18; `celigo-script-viewer.tsx` — body extracted, banner copy changed.
- `backend/scripts/seed_celigo_e2e.py` (new) + `frontend/e2e/celigo-flow-pages.spec.ts` (new).

---

### Task 1: Step names — migration, model, backfill, sync, API field

**Files:**
- Create: `backend/alembic/versions/097_celigo_flow_step_reference_name.py`
- Modify: `backend/app/models/celigo.py` (CeligoFlowStep, after `search_id`)
- Modify: `backend/app/services/celigo/repository.py` (`backfill_flow_step_reference_info`, ~line 527)
- Modify: `backend/app/services/celigo/sync_service.py` (`_process_reference_object`)
- Modify: `backend/app/api/v1/celigo_flows.py` (`CeligoFlowStepOut`, `get_flow_detail` step construction ~line 587)
- Modify: `frontend/src/hooks/use-celigo-flows.ts` (`CeligoFlowStep`)
- Test: `backend/tests/test_celigo_repository.py`, `backend/tests/test_celigo_sync.py`, `backend/tests/api/test_celigo_flows_api.py`

**Interfaces:**
- Consumes: `backfill_flow_step_reference_info(db, *, tenant_id, connection_id, celigo_id, adaptor_type, connection_celigo_id, record_type=None, operation=None, search_id=None) -> int` (bulk UPDATE, only non-None fields in SET); sanitizer already keeps `name` on export/import objects.
- Produces: `CeligoFlowStep.reference_name: Mapped[str | None]`; `backfill_flow_step_reference_info(..., reference_name: str | None = None)`; `CeligoFlowStepOut.reference_name: str | None`; TS `CeligoFlowStep.reference_name: string | null`.

- [ ] **Step 1: Write the failing repository test**

Append to `backend/tests/test_celigo_repository.py` (reuse its `_make_connection(db, tenant_id)` and `create_test_tenant` imports; construct rows with the ORM so the test owns its data):

```python
async def test_backfill_writes_reference_name_and_none_never_clobbers(db):
    tenant = await create_test_tenant(db)
    conn_id = await _make_connection(db, tenant.id)
    integration = CeligoIntegration(tenant_id=tenant.id, celigo_connection_id=conn_id, celigo_id="int_names", name="Names", raw_json={})
    db.add(integration)
    await db.flush()
    flow = CeligoFlow(tenant_id=tenant.id, celigo_connection_id=conn_id, integration_id=integration.id, celigo_id="flow_names", name="Names flow", raw_json={})
    db.add(flow)
    await db.flush()
    step = CeligoFlowStep(tenant_id=tenant.id, celigo_connection_id=conn_id, flow_id=flow.id, celigo_id="exp_names", role="generator", sequence=0, raw_json={})
    db.add(step)
    await db.flush()

    n = await backfill_flow_step_reference_info(
        db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="exp_names",
        adaptor_type="HTTPExport", connection_celigo_id=None, reference_name="Get New Sales Orders",
    )
    assert n == 1
    await db.refresh(step)
    assert step.reference_name == "Get New Sales Orders"

    # A later backfill with no name (Celigo omitted it) must not blank the stored one.
    await backfill_flow_step_reference_info(
        db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="exp_names",
        adaptor_type="HTTPExport", connection_celigo_id=None, reference_name=None,
    )
    await db.refresh(step)
    assert step.reference_name == "Get New Sales Orders"
```

- [ ] **Step 2: Run it, expect failure**

Run: `cd backend && /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_celigo_repository.py::test_backfill_writes_reference_name_and_none_never_clobbers -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'reference_name'` (and `CeligoFlowStep` has no `reference_name` attribute).

- [ ] **Step 3: Migration + model column**

Create `backend/alembic/versions/097_celigo_flow_step_reference_name.py`:

```python
"""celigo_flow_steps.reference_name -- Celigo's own export/import name on each step

Revision ID: 097_celigo_flow_step_reference_name
Revises: 094_dashboard_preference_series
"""

import sqlalchemy as sa
from alembic import op

revision = "097_celigo_flow_step_reference_name"
down_revision = "094_dashboard_preference_series"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("celigo_flow_steps", sa.Column("reference_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("celigo_flow_steps", "reference_name")
```

In `backend/app/models/celigo.py`, on `CeligoFlowStep` right after `search_id`:

```python
    reference_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The export/import's own `name` as typed in Celigo ("Get New Sales Orders",
    "Update Currency"). Lives on the REFERENCED object, so sync Phase D backfills
    it onto every step row that references that celigo_id (migration 097). NULL
    until the first post-097 sync; the UI then shows an honest fallback, never an
    invented name."""
```

Apply the migration to the LOCAL docker DB (never Supabase): `cd backend && /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m alembic upgrade head` (the alembic env reads `DATABASE_URL_DIRECT or DATABASE_URL`; both default to `localhost:5432`). Verify: `python -m alembic current` prints `097_celigo_flow_step_reference_name (head)`.

- [ ] **Step 4: Backfill parameter**

In `repository.py` `backfill_flow_step_reference_info`, add `reference_name: str | None = None` to the keyword parameters and, next to the existing non-None field assembly, `if reference_name is not None: values["reference_name"] = reference_name` (use the same dict the other optional fields go into). Extend the docstring: "`reference_name` follows the same non-None-only rule: a listing that omits the name never blanks a stored one."

- [ ] **Step 5: Run the repository test, expect pass; then write the failing sync test**

Run the Step 1 test → PASS. Append to `backend/tests/test_celigo_sync.py`:

```python
class TestStepNamesAreBackfilledFromTheReferencedObject:
    async def test_export_and_import_names_land_on_every_referencing_step(self, monkeypatch, db):
        tenant = await create_test_tenant(db)
        conn_id = await _make_connection(db, tenant.id)
        await _run_sync(
            monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id,
            integrations=[_raw_integration("int1")],
            flows=[_raw_flow("flow1", integration_id="int1", export_id="exp1")],
            exports=[_raw_export("exp1", name="Get New Sales Orders", adaptor_type="HTTPExport")],
        )
        row = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id, CeligoFlowStep.celigo_id == "exp1"))).scalar_one()
        assert row.reference_name == "Get New Sales Orders"

    async def test_a_listing_without_a_name_keeps_the_stored_one(self, monkeypatch, db):
        tenant = await create_test_tenant(db)
        conn_id = await _make_connection(db, tenant.id)
        common = dict(integrations=[_raw_integration("int1")], flows=[_raw_flow("flow1", integration_id="int1", export_id="exp1")])
        await _run_sync(monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id, exports=[_raw_export("exp1", name="Get New Sales Orders", adaptor_type="HTTPExport")], **common)
        nameless = {**_raw_export("exp1", adaptor_type="HTTPExport"), "name": None}
        await _run_sync(monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id, exports=[nameless], **common)
        row = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id, CeligoFlowStep.celigo_id == "exp1"))).scalar_one()
        assert row.reference_name == "Get New Sales Orders"
```

Run: `pytest tests/test_celigo_sync.py::TestStepNamesAreBackfilledFromTheReferencedObject -q` → FAIL (`reference_name` is None).

- [ ] **Step 6: Sync passes the name**

In `sync_service._process_reference_object`, before the `backfill_flow_step_reference_info(...)` call:

```python
    raw_name = obj.get("name")
    reference_name = raw_name if isinstance(raw_name, str) and raw_name.strip() else None
```

and pass `reference_name=reference_name` to the call. Run the two sync tests → PASS.

- [ ] **Step 7: API field (failing test first)**

In `backend/tests/api/test_celigo_flows_api.py::TestGetFlowDetail` add:

```python
    async def test_step_carries_its_celigo_name_when_synced(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        world["step"].reference_name = "Lookup Customer"
        await db.flush()
        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["steps"][0]["reference_name"] == "Lookup Customer"
```

Run → FAIL (KeyError). Add `reference_name: str | None` to `CeligoFlowStepOut` (docstring: "Celigo's own export/import name; null until synced — the UI must fall back, never invent") and `reference_name=s.reference_name` where steps are built. Run → PASS. In `use-celigo-flows.ts` `CeligoFlowStep` add `reference_name: string | null;` and add `reference_name: null` to every step literal in `frontend/src/components/settings/__tests__/celigo-flow-map.test.tsx` (`generatorStep`, `processorStep`) so `npx tsc --noEmit` stays clean.

- [ ] **Step 8: Full Celigo backend suite + lint, commit**

Run: `pytest tests/test_celigo_repository.py tests/test_celigo_sync.py tests/api/test_celigo_flows_api.py -q` → all pass. `ruff check app tests && ruff format --check app tests`.

```bash
git add backend/alembic/versions/097_celigo_flow_step_reference_name.py backend/app/models/celigo.py backend/app/services/celigo/repository.py backend/app/services/celigo/sync_service.py backend/app/api/v1/celigo_flows.py backend/tests frontend/src/hooks/use-celigo-flows.ts frontend/src/components/settings/__tests__/celigo-flow-map.test.tsx
git commit -m "feat(celigo): sync each step's own export/import name (reference_name) and serve it"
```

---

### Task 2: Fail closed on response validation; pass-through JSON fields typed as JSON

**Files:**
- Create: `backend/app/core/exception_handlers.py`
- Modify: `backend/app/main.py` (next to `add_exception_handler(CeligoManagedElsewhereError, ...)`, ~line 178)
- Modify: `backend/app/api/v1/celigo_flows.py` (`CeligoFlowStepOut.filter_json`/`mapping_json`)
- Modify: `frontend/src/hooks/use-celigo-flows.ts`
- Test: `backend/tests/test_exception_handlers.py` (new), `backend/tests/api/test_celigo_flows_api.py`

**Interfaces:**
- Produces: `register_exception_handlers(app: FastAPI) -> None`; `CeligoFlowStepOut.filter_json: JsonValue`, `mapping_json: JsonValue`; TS `CeligoJson = Record<string, unknown> | unknown[] | string | number | boolean | null`.

- [ ] **Step 1: Failing handler test**

```python
# backend/tests/test_exception_handlers.py
"""A ResponseValidationError used to escape FastAPI's ExceptionMiddleware and reach
ServerErrorMiddleware, i.e. OUTSIDE CORSMiddleware: the browser saw a 500 with no
CORS headers and reported "Failed to fetch" -- the 2026-09-01 "0 flows" incident.
The handler turns it into a JSON 500 inside the middleware stack, so headers survive."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.exception_handlers import register_exception_handlers


class Out(BaseModel):
    n: int


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["http://example.test"], allow_methods=["*"], allow_headers=["*"])
    register_exception_handlers(app)

    @app.get("/bad", response_model=Out)
    async def bad():
        return {"n": "not-an-int"}

    return app


async def test_response_validation_error_is_a_json_500_with_cors_headers():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/bad", headers={"Origin": "http://example.test"})
    assert r.status_code == 500
    assert r.json()["detail"] == "Response validation failed"
    assert r.headers.get("access-control-allow-origin") == "http://example.test"
```

Run: `pytest tests/test_exception_handlers.py -q` → FAIL (`ModuleNotFoundError: app.core.exception_handlers`).

- [ ] **Step 2: Handler**

```python
# backend/app/core/exception_handlers.py
"""Process-wide exception handlers that must run INSIDE the middleware stack.

FastAPI dispatches handlers registered here from ExceptionMiddleware, which sits
under CORSMiddleware, so the response keeps its CORS headers. An unhandled
exception instead reaches ServerErrorMiddleware, above CORS: the browser gets a
header-less 500 and reports a network error, which the UI cannot tell apart from
a real outage (2026-09-01 incident)."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def _response_validation_handler(request: Request, exc: ResponseValidationError) -> JSONResponse:
    logger.error("response validation failed path=%s errors=%s", request.url.path, exc.errors()[:5])
    return JSONResponse(status_code=500, content={"detail": "Response validation failed", "path": request.url.path})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ResponseValidationError, _response_validation_handler)
```

In `main.py`, import it and call `register_exception_handlers(application)` right after the Celigo handler line. Run the test → PASS.

- [ ] **Step 3: Failing API test for a non-dict pass-through value**

In `TestGetFlowDetail`:

```python
    async def test_a_list_shaped_filter_is_served_not_500(self, client, admin_user, db):
        """filter_json/mapping_json are opaque Celigo config relayed as-is; a shape
        nobody has seen yet must not 500 a whole flow (the schedule lesson)."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        world["step"].filter_json = ["and", ["equals", ["string", ["extract", "status"]], "open"]]
        world["step"].mapping_json = "unexpected"
        await db.flush()
        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["steps"][0]["filter_json"][0] == "and"
        assert r.json()["steps"][0]["mapping_json"] == "unexpected"
```

Run → FAIL (500 / validation). Change both fields on `CeligoFlowStepOut` to `JsonValue` with a one-line comment pointing at `CeligoSchedule`'s rationale. Run → PASS.

- [ ] **Step 4: TS type + commit**

In `use-celigo-flows.ts` add `export type CeligoJson = Record<string, unknown> | unknown[] | string | number | boolean | null;` and use it for `filter_json`/`mapping_json`. `npx tsc --noEmit` clean (fix any narrowing in `celigo-flow-map.tsx` with `typeof x === "object" && x !== null && !Array.isArray(x)` guards where it indexes fields). Run `pytest tests/test_exception_handlers.py tests/api/test_celigo_flows_api.py -q`, ruff, then:

```bash
git add backend/app/core/exception_handlers.py backend/app/main.py backend/app/api/v1/celigo_flows.py backend/tests/test_exception_handlers.py backend/tests/api/test_celigo_flows_api.py frontend/src
git commit -m "fix(api): response validation errors fail closed as JSON 500 inside CORS; celigo step JSON fields relayed as JSON"
```

---

### Task 3: Flow detail projection — step kind and facts, routers/branches, attachment family facts, Celigo's own error count

**Files:**
- Create: `backend/app/services/celigo/topology.py`
- Modify: `backend/app/api/v1/celigo_flows.py` (`CeligoAttachmentOut`, `CeligoFlowStepOut`, `CeligoFlowDetailOut`, `get_flow_detail`)
- Modify: `frontend/src/hooks/use-celigo-flows.ts`
- Test: `backend/tests/test_celigo_topology.py` (new), `backend/tests/api/test_celigo_flows_api.py`

**Interfaces:**
- Consumes: `CeligoFlow.raw_json` (sanitized; routers keep `id`, `name`, `routeRecordsTo`, `routeRecordsUsing`, `script`, `branches[]` with `branchId`, `name`, `nextRouterId`, `inputFilter.rules`, `pageProcessors`), `numOpenError`/`lastErrorAt` when present; `CeligoScript.dedup_key`, `content_hash`, `content`, `celigo_last_modified`.
- Produces (topology.py):
  - `step_kind(role: str, adaptor_type: str | None) -> str` → `"source" | "lookup" | "destination"`.
  - `count_rules(rules: object) -> int`.
  - `project_routers(raw_json: dict) -> list[dict]` — `[{id, name, route_records_to, route_records_using, has_script_slot, branches: [{id, name, rule_count, next_router_id, order, declared_step_count}]}]`.
  - `script_family_facts(scripts: list[CeligoScript]) -> dict[uuid.UUID, ScriptFamilyFact]` with `ScriptFamilyFact(name, size_chars, copies_count, versions_count, version_letter, content_diverged)`.
- Produces (API): `CeligoAttachmentOut` + `script_name: str | None`, `script_size_chars: int | None`, `script_copies_count: int | None`, `script_versions_count: int | None`, `script_version_letter: str | None`, `script_content_diverged: bool | None`; `CeligoFlowStepOut` + `kind: str`, `record_type: str | None`, `operation: str | None`, `search_id: str | None`; `CeligoRouterBranchOut`, `CeligoRouterOut`; `CeligoFlowDetailOut` + `routers: list[CeligoRouterOut]`, `celigo_open_error_count: int | None`, `last_error_at: datetime | None`.

- [ ] **Step 1: Failing pure tests**

```python
# backend/tests/test_celigo_topology.py
import uuid
from datetime import datetime, timezone

from app.models.celigo import CeligoScript
from app.services.celigo.topology import count_rules, project_routers, script_family_facts, step_kind

MULTI_SUB_RAW = {
    "routers": [
        {"id": "3e2jFK0ax5e", "name": "", "branches": [
            {"branchId": "170BOshDuyE", "name": "", "nextRouterId": "uxtwub0B7rh", "pageProcessors": [{"_exportId": "lkp"}]},
        ]},
        {"id": "uxtwub0B7rh", "name": "", "routeRecordsTo": "first_matching_branch", "routeRecordsUsing": "input_filters",
         "script": {"function": "branching"},
         "branches": [
            {"branchId": "J7gXUjQIzH4", "name": "Framework Intl", "inputFilter": {"rules": ["notequals", ["string", ["extract", "business_entity"]], "Framework Inc"]}, "pageProcessors": [{}, {}, {}, {}]},
            {"branchId": "OMcnSSbNoaU", "name": "Framework Inc", "inputFilter": {"rules": ["equals", ["string", ["extract", "business_entity"]], "Framework Inc"]}, "pageProcessors": [{}, {}, {}, {}]},
        ]},
    ]
}


def test_step_kind_follows_celigo_vocabulary():
    assert step_kind("generator", "HTTPExport") == "source"
    assert step_kind("processor", "NetSuiteExport") == "lookup"
    assert step_kind("processor", "HTTPExport") == "lookup"
    assert step_kind("processor", "NetSuiteDistributedImport") == "destination"
    assert step_kind("processor", None) == "destination"


def test_count_rules_counts_one_expression_as_one_rule():
    assert count_rules(None) == 0
    assert count_rules([]) == 0
    assert count_rules(["notequals", ["string", ["extract", "x"]], "y"]) == 1
    assert count_rules(["and", ["equals", "a", "b"], ["equals", "c", "d"]]) == 2
    assert count_rules("garbage") == 0


def test_project_routers_keeps_declared_order_chain_names_and_rule_counts():
    routers = project_routers(MULTI_SUB_RAW)
    assert [r["id"] for r in routers] == ["3e2jFK0ax5e", "uxtwub0B7rh"]
    first, second = routers
    assert first["route_records_to"] is None and first["has_script_slot"] is False
    assert first["branches"] == [{"id": "170BOshDuyE", "name": None, "rule_count": 0, "next_router_id": "uxtwub0B7rh", "order": 0, "declared_step_count": 1}]
    assert second["route_records_to"] == "first_matching_branch"
    assert second["route_records_using"] == "input_filters"
    assert second["has_script_slot"] is True
    assert [b["name"] for b in second["branches"]] == ["Framework Intl", "Framework Inc"]
    assert [b["rule_count"] for b in second["branches"]] == [1, 1]
    assert [b["order"] for b in second["branches"]] == [0, 1]


def test_project_routers_tolerates_missing_or_malformed():
    assert project_routers({}) == []
    assert project_routers({"routers": "nope"}) == []
    assert project_routers({"routers": [{"id": "r", "branches": [None, {"branchId": "b"}]}]})[0]["branches"] == [
        {"id": "b", "name": None, "rule_count": 0, "next_router_id": None, "order": 1, "declared_step_count": 0}
    ]


def _script(dedup_key, content_hash, content, modified):
    return CeligoScript(id=uuid.uuid4(), tenant_id=uuid.uuid4(), celigo_connection_id=uuid.uuid4(), celigo_id=str(uuid.uuid4()),
                        name="ns_sales_order_premap", dedup_key=dedup_key, content_hash=content_hash, content=content,
                        celigo_last_modified=datetime(2026, 1, modified, tzinfo=timezone.utc))


def test_script_family_facts_letters_versions_by_first_seen():
    fam = [_script("k", "h1", "a" * 10, 1), _script("k", "h2", "b" * 20, 2), _script("k", "h2", "b" * 20, 3), _script("k", "h3", "c" * 30, 4)]
    facts = script_family_facts(fam)
    assert facts[fam[0].id].version_letter == "A" and facts[fam[0].id].copies_count == 4 and facts[fam[0].id].versions_count == 3
    assert facts[fam[2].id].version_letter == "B" and facts[fam[2].id].content_diverged is True
    assert facts[fam[3].id].version_letter == "C" and facts[fam[3].id].size_chars == 30


def test_script_family_facts_single_copy_has_no_letter_and_is_not_diverged():
    s = _script("solo", "h", "x", 1)
    f = script_family_facts([s])[s.id]
    assert (f.copies_count, f.versions_count, f.version_letter, f.content_diverged) == (1, 1, None, False)
```

Run: `pytest tests/test_celigo_topology.py -q` → FAIL (module missing).

- [ ] **Step 2: topology.py**

```python
"""Pure projections over synced Celigo objects -- no DB, no I/O.

The sync keeps a flow's routers/branches verbatim in `celigo_flows.raw_json`
(sanitized); the step table only carries router_id/branch_id per row. The
canvas needs the DECLARED side: branch names, order, rules, the router chain
(`nextRouterId`) and the routing mode. This module is the one place that reads
those keys, so a Celigo rename is a one-file change."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.celigo import CeligoScript


def step_kind(role: str, adaptor_type: str | None) -> str:
    """Celigo's own vocabulary: Source (generator), Lookup (a processor that is an
    export), Destination (any other processor)."""
    if role == "generator":
        return "source"
    if adaptor_type and adaptor_type.lower().endswith("export"):
        return "lookup"
    return "destination"


def count_rules(rules: object) -> int:
    """A Celigo filter is one expression `[op, lhs, rhs]`; `["and"|"or", expr, ...]`
    combines several. Count expressions, not list elements."""
    if not isinstance(rules, list) or not rules:
        return 0
    head = rules[0]
    if isinstance(head, str) and head.lower() in ("and", "or"):
        return sum(1 for r in rules[1:] if isinstance(r, list))
    return 1


def project_routers(raw_json: object) -> list[dict]:
    routers = raw_json.get("routers") if isinstance(raw_json, dict) else None
    out: list[dict] = []
    for r in routers if isinstance(routers, list) else []:
        if not isinstance(r, dict):
            continue
        branches: list[dict] = []
        raw_branches = r.get("branches")
        for order, b in enumerate(raw_branches if isinstance(raw_branches, list) else []):
            if not isinstance(b, dict):
                continue
            input_filter = b.get("inputFilter")
            rules = input_filter.get("rules") if isinstance(input_filter, dict) else None
            processors = b.get("pageProcessors")
            branches.append({
                "id": b.get("branchId") or b.get("id"),
                "name": (b.get("name") or None) if isinstance(b.get("name"), str) else None,
                "rule_count": count_rules(rules),
                "next_router_id": b.get("nextRouterId") or None,
                "order": order,
                "declared_step_count": len(processors) if isinstance(processors, list) else 0,
            })
        script = r.get("script")
        out.append({
            "id": r.get("id"),
            "name": (r.get("name") or None) if isinstance(r.get("name"), str) else None,
            "route_records_to": r.get("routeRecordsTo") or None,
            "route_records_using": r.get("routeRecordsUsing") or None,
            "has_script_slot": isinstance(script, dict) and bool(script),
            "branches": branches,
        })
    return out


@dataclass(frozen=True)
class ScriptFamilyFact:
    name: str
    size_chars: int | None
    copies_count: int
    versions_count: int
    version_letter: str | None
    content_diverged: bool


def script_family_facts(scripts: list[CeligoScript]) -> dict[uuid.UUID, ScriptFamilyFact]:
    """Per script row: how many copies its clone family (`dedup_key`) has, how many
    differing versions (distinct content_hash), and which version letter THIS row
    runs -- letters assigned by the family's earliest `celigo_last_modified`
    (None sorts last), so A is the oldest text. A single-copy family gets no letter."""
    by_family: dict[str, list[CeligoScript]] = {}
    for s in scripts:
        by_family.setdefault(s.dedup_key, []).append(s)
    facts: dict[uuid.UUID, ScriptFamilyFact] = {}
    for members in by_family.values():
        ordered = sorted(members, key=lambda s: (s.celigo_last_modified is None, s.celigo_last_modified, str(s.id)))
        letters: dict[str, str] = {}
        for s in ordered:
            if s.content_hash is not None and s.content_hash not in letters:
                letters[s.content_hash] = chr(ord("A") + len(letters))
        versions = max(len(letters), 1)
        for s in members:
            letter = letters.get(s.content_hash) if len(members) > 1 and s.content_hash is not None else None
            facts[s.id] = ScriptFamilyFact(
                name=s.name, size_chars=len(s.content) if s.content is not None else None,
                copies_count=len(members), versions_count=versions, version_letter=letter,
                content_diverged=len(letters) > 1,
            )
    return facts
```

Run → PASS.

- [ ] **Step 3: Failing API test — a router-chain world**

Add a seeding helper to `backend/tests/api/test_celigo_flows_api.py` below `_seed_sandbox_world`, and reuse it in Tasks 4, 5, 6:

```python
async def _seed_router_chain_flow(db, world: dict, *, name: str = "New Sales Order to NetSuite - Multi-Subsidiary") -> dict:
    """The real Multi-Subsidiary shape: source -> router 1 (one pass-through branch
    holding a lookup with a preSavePage hook) -> router 2 (two named branches, each:
    NetSuite lookup, add customer, update customer, add salesorder with a preMap hook).
    Two scripts: one single-copy, one 3-copy family with 2 differing versions."""
    tenant_id = world["integration"].tenant_id
    conn_id = world["connection_id"]
    sfx = world["suffix"]
    flow = CeligoFlow(
        tenant_id=tenant_id, celigo_connection_id=conn_id, integration_id=world["integration"].id,
        celigo_id=f"flow_chain_{sfx}", name=name, disabled=False,
        schedule="? 5,20,35,50 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23 ? * *", timezone="America/Los_Angeles",
        last_executed_at=datetime(2026, 9, 2, 17, 51, tzinfo=timezone.utc), celigo_last_modified=datetime(2026, 9, 2, tzinfo=timezone.utc),
        raw_json={"numOpenError": 0, "lastErrorAt": None, "routers": [
            {"id": "r1", "name": "", "branches": [{"branchId": "b0", "name": "", "nextRouterId": "r2", "pageProcessors": [{"_exportId": f"lkp_{sfx}"}]}]},
            {"id": "r2", "name": "", "routeRecordsTo": "first_matching_branch", "routeRecordsUsing": "input_filters", "branches": [
                {"branchId": "bIntl", "name": "Framework Intl", "inputFilter": {"rules": ["notequals", ["string", ["extract", "business_entity"]], "Framework Inc"]}, "pageProcessors": [{}, {}, {}, {}]},
                {"branchId": "bInc", "name": "Framework Inc", "inputFilter": {"rules": ["equals", ["string", ["extract", "business_entity"]], "Framework Inc"]}, "pageProcessors": [{}, {}, {}, {}]},
            ]},
        ]},
    )
    db.add(flow)
    await db.flush()

    def step(celigo_id, role, adaptor, *, router=None, branch=None, seq=0, record_type=None, operation=None, search_id=None, reference_name=None, mapping=None):
        return CeligoFlowStep(tenant_id=tenant_id, celigo_connection_id=conn_id, flow_id=flow.id, celigo_id=celigo_id, role=role,
                              adaptor_type=adaptor, router_id=router, branch_id=branch, sequence=seq, record_type=record_type,
                              operation=operation, search_id=search_id, reference_name=reference_name, mapping_json=mapping, raw_json={})

    steps = [step(f"src_{sfx}", "generator", "HTTPExport", reference_name="Get New Sales Orders"),
             step(f"lkp_{sfx}", "processor", "HTTPExport", router="r1", branch="b0", reference_name="Lookup Sales Orders (Multi-Subsidiary)", mapping={"fields": [{"extract": "a", "generate": "b"}] * 23})]
    for branch, suffix in (("bIntl", "BV"), ("bInc", "Inc")):
        steps += [
            step(f"cust_lkp_{branch}_{sfx}", "processor", "NetSuiteExport", router="r2", branch=branch, seq=0, record_type="customer", search_id="5090", reference_name="Lookup Customer"),
            step(f"cust_add_{branch}_{sfx}", "processor", "NetSuiteDistributedImport", router="r2", branch=branch, seq=1, record_type="customer", operation="add", reference_name=f"Import Customer ({suffix})"),
            step(f"cust_upd_{branch}_{sfx}", "processor", "NetSuiteDistributedImport", router="r2", branch=branch, seq=2, record_type="customer", operation="update", reference_name="Update Currency"),
            step(f"so_add_{branch}_{sfx}", "processor", "NetSuiteDistributedImport", router="r2", branch=branch, seq=3, record_type="salesorder", operation="add", reference_name=f"Add New Sales Order ({suffix})"),
        ]
    db.add_all(steps)
    await db.flush()

    solo = CeligoScript(tenant_id=tenant_id, celigo_connection_id=conn_id, celigo_id=f"scr_solo_{sfx}", name="sales_order_script_v2", content="x" * 34145, content_hash="hsolo", celigo_last_modified=datetime(2026, 8, 25, tzinfo=timezone.utc))
    fam = [CeligoScript(tenant_id=tenant_id, celigo_connection_id=conn_id, celigo_id=f"scr_fam{i}_{sfx}", source_id=f"scr_fam0_{sfx}" if i else None, name="ns_sales_order_premap",
                        content=c, content_hash=h, celigo_last_modified=datetime(2026, 1, 1 + i, tzinfo=timezone.utc))
           for i, (c, h) in enumerate((("a" * 2284, "hA"), ("b" * 2443, "hB"), ("b" * 2443, "hB")))]
    db.add_all([solo, *fam])
    await db.flush()
    by_id = {s.celigo_id: s for s in steps}
    atts = [CeligoScriptAttachment(tenant_id=tenant_id, celigo_connection_id=conn_id, flow_id=flow.id, flow_step_id=by_id[f"lkp_{sfx}"].id, script_id=solo.id, script_celigo_id=solo.celigo_id, function_name="preSavePage", json_path=f"lkp_{sfx}.hooks.preSavePage", site_type="hook")]
    for branch in ("bIntl", "bInc"):
        atts.append(CeligoScriptAttachment(tenant_id=tenant_id, celigo_connection_id=conn_id, flow_id=flow.id, flow_step_id=by_id[f"so_add_{branch}_{sfx}"].id, script_id=fam[1].id, script_celigo_id=fam[1].celigo_id, function_name="preMap", json_path=f"so_add_{branch}_{sfx}.hooks.preMap", site_type="hook"))
    db.add_all(atts)
    await db.flush()
    return {"flow": flow, "steps": by_id, "solo": solo, "family": fam, "attachments": atts}
```

(`source_id` on `CeligoScript` drives the generated `dedup_key`: the three family rows share `dedup_key = scr_fam0_<sfx>`.) Then the test in `TestGetFlowDetail`:

```python
    async def test_detail_projects_kinds_facts_routers_and_script_families(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        r = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        kinds = {s["celigo_id"]: s["kind"] for s in body["steps"]}
        sfx = world["suffix"]
        assert kinds[f"src_{sfx}"] == "source" and kinds[f"lkp_{sfx}"] == "lookup" and kinds[f"cust_lkp_bIntl_{sfx}"] == "lookup" and kinds[f"so_add_bInc_{sfx}"] == "destination"
        so = next(s for s in body["steps"] if s["celigo_id"] == f"so_add_bIntl_{sfx}")
        assert (so["record_type"], so["operation"], so["search_id"], so["reference_name"]) == ("salesorder", "add", None, "Add New Sales Order (BV)")
        lk = next(s for s in body["steps"] if s["celigo_id"] == f"cust_lkp_bIntl_{sfx}")
        assert (lk["record_type"], lk["search_id"]) == ("customer", "5090")
        assert [rt["id"] for rt in body["routers"]] == ["r1", "r2"]
        assert body["routers"][0]["branches"][0]["next_router_id"] == "r2"
        assert [b["name"] for b in body["routers"][1]["branches"]] == ["Framework Intl", "Framework Inc"]
        assert body["routers"][1]["branches"][0]["rule_count"] == 1
        assert body["celigo_open_error_count"] == 0 and body["last_error_at"] is None
        hook = next(s for s in body["steps"] if s["celigo_id"] == f"so_add_bInc_{sfx}")["attachments"][0]
        assert (hook["script_name"], hook["script_copies_count"], hook["script_versions_count"], hook["script_version_letter"], hook["script_content_diverged"]) == ("ns_sales_order_premap", 3, 2, "B", True)
        solo = next(s for s in body["steps"] if s["celigo_id"] == f"lkp_{sfx}")["attachments"][0]
        assert (solo["script_copies_count"], solo["script_version_letter"], solo["script_size_chars"]) == (1, None, 34145)
```

Add the needed imports (`datetime`, `timezone`, `CeligoScript`, `CeligoScriptAttachment` are already imported by `_seed_world`; check). Run → FAIL (`KeyError: 'kind'`).

- [ ] **Step 4: Response models + get_flow_detail**

In `celigo_flows.py`:

```python
class CeligoRouterBranchOut(BaseModel):
    id: str | None
    name: str | None
    rule_count: int
    next_router_id: str | None
    order: int
    declared_step_count: int


class CeligoRouterOut(BaseModel):
    """Projected from the synced flow object by `topology.project_routers` -- the
    declared side of branching (names, order, rules, chain, mode) that step rows
    cannot carry."""
    id: str | None
    name: str | None
    route_records_to: str | None
    route_records_using: str | None
    has_script_slot: bool
    branches: list[CeligoRouterBranchOut]
```

`CeligoAttachmentOut` gains the six `script_*` fields (all `| None`, default `None`). `CeligoFlowStepOut` gains `kind: str`, `record_type: str | None`, `operation: str | None`, `search_id: str | None`. `CeligoFlowDetailOut` gains `routers: list[CeligoRouterOut]`, `celigo_open_error_count: int | None`, `last_error_at: datetime | None`.

In `get_flow_detail`, after loading attachments: collect `script_ids = {a.script_id for a in attachments if a.script_id}`; if any, `dedup_keys = select(CeligoScript.dedup_key).where(CeligoScript.tenant_id == tenant_id, CeligoScript.id.in_(script_ids))`; load `family_rows = select(CeligoScript).where(CeligoScript.tenant_id == tenant_id, CeligoScript.celigo_connection_id == flow.celigo_connection_id, CeligoScript.dedup_key.in_(dedup_keys), celigo_script_is_production())`; `facts = script_family_facts(family_rows)`; when building each attachment out, look up `facts.get(a.script_id)` and copy its fields. Steps: `kind=step_kind(s.role, s.adaptor_type)`, plus the three fact columns. Detail: `routers=[CeligoRouterOut(**r) for r in project_routers(flow.raw_json)]`, `celigo_open_error_count = flow.raw_json.get("numOpenError") if isinstance(flow.raw_json.get("numOpenError"), int) else None`, `last_error_at = _parse_iso(flow.raw_json.get("lastErrorAt"))` (write a 6-line `_parse_iso(value) -> datetime | None` next to the models: `datetime.fromisoformat(value.replace("Z", "+00:00"))` inside try/except for str input, else None). Run → PASS.

- [ ] **Step 5: TS types, suite, commit**

`use-celigo-flows.ts`: `CeligoAttachment` + the six `script_*` fields (`| null`); `CeligoFlowStep` + `kind: "source" | "lookup" | "destination"`, `record_type`, `operation`, `search_id` (`string | null`); new `CeligoRouterBranch`, `CeligoRouter`; `CeligoFlowDetail` + `routers: CeligoRouter[]`, `celigo_open_error_count: number | null`, `last_error_at: string | null`. Update the step/attachment literals in `celigo-flow-map.test.tsx` (`kind`, three facts, script fields as null). `npx tsc --noEmit`. Run `pytest tests/test_celigo_topology.py tests/api/test_celigo_flows_api.py -q`, ruff.

```bash
git add backend/app/services/celigo/topology.py backend/app/api/v1/celigo_flows.py backend/tests frontend/src
git commit -m "feat(celigo): flow detail projects step kind/facts, declared routers and branches, and script family state"
```

---

### Task 4: Errors per step and per flow; grouped flow errors route

**Files:**
- Modify: `backend/app/api/v1/celigo_flows.py`
- Modify: `frontend/src/hooks/use-celigo-flows.ts`
- Test: `backend/tests/api/test_celigo_flows_api.py`

**Interfaces:**
- Consumes: `celigo_error_is_open()`; `CeligoFlowError` (`flow_id`, `flow_step_id`, `signature_id`, `trace_key`, `occurred_at`, `purge_at`, `retriable`, `resolved_at`); `CeligoErrorSignatureOut`, `CeligoErrorOut` (existing).
- Produces: `CeligoFlowStepOut.error_count: int`; `CeligoFlowDetailOut.error_count: int`, `signature_count: int`; `GET /celigo/flows/{flow_id}/errors?status=open|resolved&limit=100` → `CeligoFlowErrorsOut{flow_id, status, total, groups: [CeligoFlowErrorGroupOut{signature: CeligoErrorSignatureOut | None, count, step_ids: list[str | None], first_seen_at, last_seen_at, retriable, purge_at, trace_keys: list[str], errors: list[CeligoErrorOut]}]}`; TS `useCeligoFlowErrors(flowId?: string, status: "open" | "resolved" = "open")`.

- [ ] **Step 1: Failing tests**

```python
class TestFlowErrors:
    async def _seed_two_step_errors(self, db, world, chain):
        sfx = world["suffix"]
        tenant_id = world["integration"].tenant_id
        sig = world["signature"]
        rows = []
        for i, step_key in enumerate((f"lkp_{sfx}", f"lkp_{sfx}", f"so_add_bIntl_{sfx}")):
            rows.append(CeligoFlowError(tenant_id=tenant_id, celigo_connection_id=world["connection_id"], flow_id=chain["flow"].id,
                                        flow_step_id=chain["steps"][step_key].id, signature_id=sig.id, celigo_id=f"err_{i}_{sfx}",
                                        trace_key=f"1582211{i}", source="pre_save_page_hook", code="script_error", message="TypeError: null",
                                        occurred_at=datetime(2026, 8, 17, 6 + i, tzinfo=timezone.utc), purge_at=datetime(2026, 9, 16, tzinfo=timezone.utc), retriable=False))
        rows.append(CeligoFlowError(tenant_id=tenant_id, celigo_connection_id=world["connection_id"], flow_id=chain["flow"].id, flow_step_id=chain["steps"][f"lkp_{sfx}"].id,
                                    signature_id=sig.id, celigo_id=f"err_resolved_{sfx}", source="pre_save_page_hook", code="script_error", message="old",
                                    occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc), resolved_at=datetime(2026, 8, 2, tzinfo=timezone.utc)))
        db.add_all(rows)
        await db.flush()

    async def test_detail_carries_open_counts_per_step_and_per_flow(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        body = (await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}", headers=headers)).json()
        sfx = world["suffix"]
        counts = {s["celigo_id"]: s["error_count"] for s in body["steps"]}
        assert counts[f"lkp_{sfx}"] == 2 and counts[f"so_add_bIntl_{sfx}"] == 1 and counts[f"src_{sfx}"] == 0
        assert body["error_count"] == 3 and body["signature_count"] == 1

    async def test_grouped_errors_by_signature_with_step_attribution_and_trace_keys(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        r = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "open" and body["total"] == 3 and len(body["groups"]) == 1
        g = body["groups"][0]
        assert g["signature"]["id"] == str(world["signature"].id)
        assert g["count"] == 3 and sorted(g["trace_keys"]) == ["15822110", "15822111", "15822112"]
        assert set(g["step_ids"]) == {str(chain["steps"][f"lkp_{world['suffix']}"].id), str(chain["steps"][f"so_add_bIntl_{world['suffix']}"].id)}
        assert g["first_seen_at"].startswith("2026-08-17T06") and g["last_seen_at"].startswith("2026-08-17T08")
        assert g["retriable"] is False and g["purge_at"].startswith("2026-09-16")
        assert "message" in g["errors"][0]

    async def test_resolved_filter_and_404s(self, client, admin_user, admin_user_b, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        r = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors?status=resolved", headers=headers)
        assert r.status_code == 200 and r.json()["total"] == 1
        assert (await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors?status=bogus", headers=headers)).status_code == 422
        assert (await client.get(f"/api/v1/celigo/flows/{uuid.uuid4()}/errors", headers=headers)).status_code == 404
        _, headers_b = admin_user_b
        assert (await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors", headers=headers_b)).status_code == 404
```

Run → FAIL.

- [ ] **Step 2: Implement**

Per-step counts in `get_flow_detail`: one query `select(CeligoFlowError.flow_step_id, func.count(), func.count(distinct(CeligoFlowError.signature_id))).where(CeligoFlowError.tenant_id == tenant_id, CeligoFlowError.flow_id == flow.id, celigo_error_is_open()).group_by(CeligoFlowError.flow_step_id)`; step `error_count` from the map (default 0); flow `error_count` = sum, `signature_count` = distinct signatures over the flow (a second small query with the same predicate and no group by). New route:

```python
class CeligoFlowErrorGroupOut(BaseModel):
    signature: CeligoErrorSignatureOut | None
    count: int
    step_ids: list[str | None]
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    retriable: bool | None
    purge_at: datetime | None
    trace_keys: list[str]
    errors: list[CeligoErrorOut]


class CeligoFlowErrorsOut(BaseModel):
    flow_id: str
    status: Literal["open", "resolved"]
    total: int
    groups: list[CeligoFlowErrorGroupOut]


@router.get("/flows/{flow_id}/errors", response_model=CeligoFlowErrorsOut)
async def list_flow_errors(flow_id: uuid.UUID, user=..., _flag=..., db=..., status: Literal["open", "resolved"] = "open", limit: int = Query(100, ge=1, le=500)):
```

Load the flow via the same production join as `get_flow_detail` (404 otherwise). Predicate: open → `celigo_error_is_open()`; resolved → `CeligoFlowError.resolved_at.isnot(None)`. Query errors ordered by `occurred_at desc nulls last` (no limit for grouping; cap at 2000 rows with a comment). Group in Python by `signature_id`; load signatures in one `in_` query; per group: `count`, `step_ids` = ordered distinct `flow_step_id` strings, `first_seen_at`/`last_seen_at` = min/max `occurred_at`, `retriable` = `False` if any False else `True` if any True else None, `purge_at` = min non-null, `trace_keys` = first 25 distinct non-null, `errors` = first `limit` rows. Groups ordered by count desc. `total` = number of rows. Run → PASS.

- [ ] **Step 3: TS hook, commit**

```ts
export interface CeligoFlowErrorGroup { signature: CeligoErrorSignature | null; count: number; step_ids: (string | null)[]; first_seen_at: string | null; last_seen_at: string | null; retriable: boolean | null; purge_at: string | null; trace_keys: string[]; errors: CeligoError[]; }
export interface CeligoFlowErrors { flow_id: string; status: "open" | "resolved"; total: number; groups: CeligoFlowErrorGroup[]; }
export function useCeligoFlowErrors(flowId: string | undefined, status: "open" | "resolved" = "open") {
  return useQuery<CeligoFlowErrors>({ queryKey: ["celigo", "flow", flowId, "errors", status], queryFn: () => apiClient.get<CeligoFlowErrors>(`/api/v1/celigo/flows/${flowId}/errors?status=${status}`), enabled: !!flowId });
}
```

(`CeligoErrorSignature`/`CeligoError` mirror `CeligoErrorSignatureOut`/`CeligoErrorOut` field-for-field; add them if absent.) Add `error_count: number` to TS `CeligoFlowStep` and `error_count`/`signature_count` to `CeligoFlowDetail`; update test literals. Run the API test file, ruff, tsc.

```bash
git add backend/app/api/v1/celigo_flows.py backend/tests/api/test_celigo_flows_api.py frontend/src
git commit -m "feat(celigo): open-error counts per step and per flow; grouped flow errors route"
```

---

### Task 5: Flow list aggregates (table columns in one request)

**Files:**
- Modify: `backend/app/api/v1/celigo_flows.py` (`CeligoFlowSummaryOut`, `list_integration_flows`)
- Modify: `frontend/src/hooks/use-celigo-flows.ts`
- Test: `backend/tests/api/test_celigo_flows_api.py`

**Interfaces:**
- Produces: `CeligoRecordWriteOut{record_type: str, count: int}`; `CeligoFlowSummaryOut` + `step_count`, `router_count`, `branch_count`, `lookup_count`, `script_count`, `diverged_family_count` (ints), `writes: list[CeligoRecordWriteOut]`, `celigo_last_modified: datetime | None`.

- [ ] **Step 1: Failing test** (in `TestListIntegrationFlows`)

```python
    async def test_lists_topology_script_and_write_aggregates_per_flow(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200
        row = next(f for f in r.json() if f["id"] == str(chain["flow"].id))
        assert (row["step_count"], row["router_count"], row["branch_count"], row["lookup_count"]) == (10, 2, 3, 3)
        assert (row["script_count"], row["diverged_family_count"]) == (2, 1)
        assert sorted(row["writes"], key=lambda w: w["record_type"]) == [{"record_type": "customer", "count": 4}, {"record_type": "salesorder", "count": 2}]
        assert row["celigo_last_modified"].startswith("2026-09-02")
        base = next(f for f in r.json() if f["id"] == str(world["flow"].id))
        assert base["writes"] == [] or all("count" in w for w in base["writes"])
```

Run → FAIL.

- [ ] **Step 2: Implement with three grouped queries (never per flow)**

After the existing error-count query in `list_integration_flows` (flow ids known):

```python
    topo = (await db.execute(
        select(CeligoFlowStep.flow_id, func.count().label("steps"), func.count(distinct(CeligoFlowStep.router_id)).label("routers"),
               func.count(distinct(CeligoFlowStep.branch_id)).label("branches"),
               func.count().filter(and_(CeligoFlowStep.role == "processor", CeligoFlowStep.adaptor_type.ilike("%export"))).label("lookups"))
        .where(CeligoFlowStep.tenant_id == tenant_id, CeligoFlowStep.flow_id.in_(flow_ids)).group_by(CeligoFlowStep.flow_id)
    )).all()
    writes = (await db.execute(
        select(CeligoFlowStep.flow_id, CeligoFlowStep.record_type, func.count())
        .where(CeligoFlowStep.tenant_id == tenant_id, CeligoFlowStep.flow_id.in_(flow_ids), CeligoFlowStep.record_type.isnot(None), CeligoFlowStep.operation.isnot(None))
        .group_by(CeligoFlowStep.flow_id, CeligoFlowStep.record_type).order_by(func.count().desc(), CeligoFlowStep.record_type)
    )).all()
    diverged_keys = (select(CeligoScript.dedup_key).where(CeligoScript.tenant_id == tenant_id, CeligoScript.celigo_connection_id == connection.id, CeligoScript.content_hash.isnot(None))
                     .group_by(CeligoScript.dedup_key).having(func.count(distinct(CeligoScript.content_hash)) > 1))
    scripts = (await db.execute(
        select(CeligoScriptAttachment.flow_id, func.count(distinct(CeligoScriptAttachment.script_id)).label("scripts"),
               func.count(distinct(CeligoScript.dedup_key)).filter(CeligoScript.dedup_key.in_(diverged_keys)).label("diverged"))
        .join(CeligoScript, CeligoScript.id == CeligoScriptAttachment.script_id)
        .where(CeligoScriptAttachment.tenant_id == tenant_id, CeligoScriptAttachment.flow_id.in_(flow_ids), celigo_script_is_production())
        .group_by(CeligoScriptAttachment.flow_id)
    )).all()
```

Fold into dicts keyed by `flow_id` and populate the new fields (defaults 0 / `[]`). Run → PASS.

- [ ] **Step 3: TS, commit**

`CeligoFlowSummary` + `step_count`, `router_count`, `branch_count`, `lookup_count`, `script_count`, `diverged_family_count: number`, `writes: {record_type: string; count: number}[]`, `celigo_last_modified: string | null`; update flow literals in `celigo-flow-map.test.tsx`. Run tests, ruff, tsc.

```bash
git add backend/app/api/v1/celigo_flows.py backend/tests/api/test_celigo_flows_api.py frontend/src
git commit -m "feat(celigo): flow list carries topology, script and writes aggregates in one request"
```

---

### Task 6: Integration summaries on the list (one request for the dashboard)

**Files:**
- Modify: `backend/app/models/celigo.py` (new predicate)
- Modify: `backend/app/api/v1/celigo_flows.py` (`CeligoIntegrationOut`, `list_integrations`)
- Modify: `frontend/src/hooks/use-celigo-flows.ts`
- Test: `backend/tests/api/test_celigo_flows_api.py`, `backend/tests/test_celigo_repository.py` (predicate)

**Interfaces:**
- Produces: `celigo_flow_is_on_demand() -> ColumnElement[bool]` in models (`schedule IS NULL OR jsonb_typeof(schedule) = 'null' OR (jsonb_typeof(schedule) = 'string' AND schedule #>> '{}' = '')`); `CeligoFlowScheduleOut{id, name, disabled, schedule: CeligoSchedule, last_executed_at}`; `CeligoIntegrationOut` + `flow_count`, `scheduled_count`, `on_demand_count`, `paused_count`, `step_count`, `router_count`, `lookup_count`, `script_count`, `no_run_count`, `error_count`, `changes_last_24h` (ints), `last_run_at: datetime | None`, `writes: list[CeligoRecordWriteOut]`, `adaptor_families: list[str]`, `flow_schedules: list[CeligoFlowScheduleOut]`.

- [ ] **Step 1: Failing tests**

Predicate test in `test_celigo_repository.py`:

```python
async def test_celigo_flow_is_on_demand_treats_json_null_and_empty_string_as_on_demand(db):
    tenant = await create_test_tenant(db)
    conn_id = await _make_connection(db, tenant.id)
    integration = CeligoIntegration(tenant_id=tenant.id, celigo_connection_id=conn_id, celigo_id="int_od", name="OD", raw_json={})
    db.add(integration)
    await db.flush()
    def flow(cid, schedule):
        return CeligoFlow(tenant_id=tenant.id, celigo_connection_id=conn_id, integration_id=integration.id, celigo_id=cid, name=cid, schedule=schedule, raw_json={})
    db.add_all([flow("od_sqlnull", None), flow("od_jsonnull", sa_null_json()), flow("od_empty", ""), flow("cron", "? 5 * ? * *")])
    await db.flush()
    ids = set((await db.execute(select(CeligoFlow.celigo_id).where(CeligoFlow.tenant_id == tenant.id, celigo_flow_is_on_demand()))).scalars())
    assert ids == {"od_sqlnull", "od_jsonnull", "od_empty"}
```

where `sa_null_json()` is `from sqlalchemy import JSON; JSON.NULL` (SQLAlchemy's JSON null sentinel — writes the JSON `null` value, not SQL NULL). API test in `TestListIntegrations`:

```python
    async def test_lists_summary_counts_writes_families_and_flow_schedules(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        paused = await _seed_cron_flow(db, world, name="Paused one")
        paused.disabled = True
        await db.flush()
        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 200
        row = next(i for i in r.json() if i["id"] == str(world["integration"].id))
        assert row["flow_count"] == 3 and row["paused_count"] == 1 and row["scheduled_count"] >= 1
        assert row["scheduled_count"] + row["on_demand_count"] + row["paused_count"] == row["flow_count"]
        assert row["step_count"] >= 10 and row["router_count"] >= 2 and row["lookup_count"] >= 3
        assert row["script_count"] >= 2 and row["error_count"] >= 0 and row["changes_last_24h"] == 0
        assert row["last_run_at"].startswith("2026-09-02T17:51")
        assert {"record_type": "salesorder", "count": 2} in row["writes"]
        assert "NetSuite" in row["adaptor_families"] and "HTTP" in row["adaptor_families"]
        sched = next(f for f in row["flow_schedules"] if f["id"] == str(chain["flow"].id))
        assert sched["disabled"] is False and sched["schedule"].startswith("? 5,20,35,50") and sched["last_executed_at"] is not None
```

Run → FAIL.

- [ ] **Step 2: Implement**

Models:

```python
def celigo_flow_is_on_demand() -> ColumnElement[bool]:
    """A flow with no schedule runs on demand. Celigo sends the schedule as a
    cron STRING, JSON null, or "" -- all three mean on demand; SQL NULL too."""
    typeof = func.jsonb_typeof(CeligoFlow.schedule)
    return or_(CeligoFlow.schedule.is_(None), typeof == "null", and_(typeof == "string", CeligoFlow.schedule.astext == ""))
```

In `list_integrations`, after selecting production integrations (ids known):
- flows query: `select(CeligoFlow.integration_id, CeligoFlow.id, CeligoFlow.name, CeligoFlow.disabled, CeligoFlow.schedule, CeligoFlow.last_executed_at).where(tenant, integration_id.in_(ids))` → per integration: `flow_count`, `paused_count` (disabled is True), `on_demand_count` (not paused and `_is_on_demand_value(schedule)` evaluated in Python with the same three cases — keep the Python twin next to the predicate with a comment naming it; the DB predicate is used by the changes/other queries), `scheduled_count` = rest, `no_run_count`, `last_run_at`, `flow_schedules`.
- steps query joined to flows grouped by `integration_id`: `count`, `count(distinct router_id)`, lookups filter, `array_agg(distinct adaptor_type)`; families via a small `adaptor_family(adaptor_type) -> str | None` helper in `topology.py` (`NetSuite` if "netsuite" in it, then `AS2`, `FTP`, `RDBMS`, `REST`, `HTTP`; else None) with a unit test in `test_celigo_topology.py`.
- writes query joined to flows grouped by `(integration_id, record_type)` with the `record_type`/`operation` not-null filter.
- scripts query: attachments join flows, `count(distinct script_id)` grouped by integration.
- errors query: `CeligoFlowError` join `CeligoFlow` with `celigo_error_is_open()` grouped by integration.
- changes: `CeligoConfigChange` join `CeligoFlow` where `created_at >= func.now() - text("interval '24 hours'")` grouped by integration.
Run → PASS.

- [ ] **Step 3: TS, commit**

`CeligoIntegration` + the new fields (`flow_schedules: CeligoFlowSchedule[]` with `CeligoFlowSchedule{id,name,disabled,schedule,last_executed_at}`); update the `integration` literal in `celigo-flow-map.test.tsx`. Tests, ruff, tsc.

```bash
git add backend/app/models/celigo.py backend/app/api/v1/celigo_flows.py backend/tests frontend/src
git commit -m "feat(celigo): integration list carries dashboard summaries and flow schedules in one request"
```

---

### Task 7: Config-change routes

**Files:**
- Modify: `backend/app/api/v1/celigo_flows.py`
- Modify: `frontend/src/hooks/use-celigo-flows.ts`
- Test: `backend/tests/api/test_celigo_flows_api.py`

**Interfaces:**
- Produces: `CeligoConfigChangeOut{id, object_kind, object_id: str | None, celigo_id, field, old_value: JsonValue, new_value: JsonValue, flow_id: str | None, created_at: datetime}`; `GET /celigo/integrations/{id}/changes?limit=200` and `GET /celigo/flows/{id}/changes?limit=200` → `list[CeligoConfigChangeOut]` newest first; TS `useCeligoIntegrationChanges(id?)`, `useCeligoFlowChanges(id?)`.

- [ ] **Step 1: Failing tests**

```python
class TestChanges:
    async def test_integration_and_flow_changes_newest_first(self, client, admin_user, admin_user_b, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        older = CeligoConfigChange(tenant_id=user.tenant_id, celigo_connection_id=world["connection_id"], flow_id=world["flow"].id, object_kind="flow", object_id=world["flow"].id, celigo_id=world["flow"].celigo_id, field="disabled", old_value=False, new_value=True)
        newer = CeligoConfigChange(tenant_id=user.tenant_id, celigo_connection_id=world["connection_id"], flow_id=world["flow"].id, object_kind="flow_step", object_id=world["step"].id, celigo_id=world["step"].celigo_id, field="mapping_json", old_value={"a": 1}, new_value=["x"])
        db.add_all([older, newer])
        await db.flush()
        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/changes", headers=headers)
        assert r.status_code == 200 and [c["field"] for c in r.json()] == ["mapping_json", "disabled"]
        assert r.json()[0]["new_value"] == ["x"]
        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}/changes", headers=headers)
        assert r.status_code == 200 and len(r.json()) == 2
        assert (await client.get(f"/api/v1/celigo/integrations/{uuid.uuid4()}/changes", headers=headers)).status_code == 404
        _, headers_b = admin_user_b
        assert (await client.get(f"/api/v1/celigo/flows/{world['flow'].id}/changes", headers=headers_b)).status_code == 404
```

(If `created_at` ties inside one flush, order the two `db.add` calls with a `await db.flush()` between them.) Run → FAIL.

- [ ] **Step 2: Implement**

Both routes: resolve the integration/flow through the production join (404); `select(CeligoConfigChange).where(tenant, CeligoConfigChange.flow_id.in_(flow_ids_of_integration) / == flow.id).order_by(CeligoConfigChange.created_at.desc(), CeligoConfigChange.id.desc()).limit(limit)`. Run → PASS.

- [ ] **Step 3: TS hooks, commit**

```ts
export interface CeligoConfigChange { id: string; object_kind: string; object_id: string | null; celigo_id: string; field: string; old_value: CeligoJson; new_value: CeligoJson; flow_id: string | null; created_at: string; }
export function useCeligoIntegrationChanges(integrationId: string | undefined) { return useQuery<CeligoConfigChange[]>({ queryKey: ["celigo", "integration", integrationId, "changes"], queryFn: () => apiClient.get<CeligoConfigChange[]>(`/api/v1/celigo/integrations/${integrationId}/changes`), enabled: !!integrationId }); }
export function useCeligoFlowChanges(flowId: string | undefined) { return useQuery<CeligoConfigChange[]>({ queryKey: ["celigo", "flow", flowId, "changes"], queryFn: () => apiClient.get<CeligoConfigChange[]>(`/api/v1/celigo/flows/${flowId}/changes`), enabled: !!flowId }); }
```

```bash
git add backend/app/api/v1/celigo_flows.py backend/tests/api/test_celigo_flows_api.py frontend/src/hooks/use-celigo-flows.ts
git commit -m "feat(celigo): config-change routes per integration and per flow"
```

---

### Task 8: Frontend foundation — schedule.ts, shared.tsx, chips.ts

**Files:**
- Create: `frontend/src/components/celigo/schedule.ts`, `frontend/src/components/celigo/shared.tsx`, `frontend/src/components/celigo/chips.ts`
- Test: `frontend/src/components/celigo/__tests__/schedule.test.ts`, `__tests__/chips.test.ts`, `__tests__/shared.test.tsx`

**Interfaces (produces):**
```ts
// schedule.ts
export type ParsedSchedule = { kind: "cron"; cron: string; intervalMinutes: number | null; label: string; display: string } | { kind: "on_demand" } | { kind: "unknown"; raw: string };
export function parseSchedule(schedule: CeligoSchedule): ParsedSchedule;
export type StallState = { state: "on_time" | "stalled" | "paused" | "on_demand" | "no_run" | "unknown"; missedRuns?: number; intervalMinutes?: number };
export function stallState(args: { schedule: CeligoSchedule; disabled: boolean | null; lastExecutedAt: string | null; lastSyncedAt: string | null }): StallState;
// shared.tsx
export function formatRelativeTime(iso: string | null, now?: Date): string;   // "2 min ago", "3 h ago", "6 days ago", "17 months ago", "—" for null
export function adaptorFamily(adaptorType: string | null): "NetSuite" | "HTTP" | "AS2" | "FTP" | "RDBMS" | "REST" | null;
export function fallbackStepTitle(step: Pick<CeligoFlowStep, "kind" | "adaptor_type" | "record_type" | "operation" | "search_id">): { text: string; unsynced: boolean };
export function deriveFlowSummary(detail: CeligoFlowDetail): string;
export function ErrorNotice(props: { message: string; onRetry?: () => void }): JSX.Element;
export function Pill(props: { tone: "ok" | "crit" | "warn" | "mute" | "accent"; dot?: "solid" | "hollow"; children: React.ReactNode; title?: string }): JSX.Element;
export function ErrorPill(props: { count: number; signatureCount?: number; checkedAt: string | null }): JSX.Element;   // "0 open errors · checked 4 min ago" / "10 open · 1 root cause"
export function SchedulePill(props: { stall: StallState; parsed: ParsedSchedule }): JSX.Element;
export function Medallions(props: { families: string[] }): JSX.Element;
// chips.ts
export type Chip = { slot: "transform" | "hooks" | "output_filter" | "input_filter" | "ns_mapping" | "response_mapping"; state: "configured" | "none" | "unsynced"; label: string; attachmentId?: string; functionName?: string; versionLetter?: string | null; versionsCount?: number | null; copiesCount?: number | null; diverged?: boolean };
export function affordanceChips(step: CeligoFlowStep): Chip[];
```

- [ ] **Step 1: Failing schedule tests**

```ts
// frontend/src/components/celigo/__tests__/schedule.test.ts
import { describe, expect, it } from "vitest";
import { parseSchedule, stallState } from "../schedule";

const HOURS = "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23";

describe("parseSchedule — only the shapes seen live are humanised", () => {
  it("minute list over every hour", () => {
    const p = parseSchedule(`? 5,20,35,50 ${HOURS} ? * *`);
    expect(p).toMatchObject({ kind: "cron", intervalMinutes: 15, label: "every 15 min", display: "? 5,20,35,50 0…23 ? * *" });
  });
  it("two minutes an hour, single minute an hour, hour lists, */N", () => {
    expect(parseSchedule(`? 5,35 ${HOURS} ? * *`)).toMatchObject({ intervalMinutes: 30, label: "every 30 min" });
    expect(parseSchedule(`? 10 ${HOURS} ? * *`)).toMatchObject({ intervalMinutes: 60, label: "hourly at :10" });
    expect(parseSchedule("? 5 0,4,8,12,16,20 ? * *")).toMatchObject({ intervalMinutes: 240, label: "every 4 h" });
    expect(parseSchedule("? 5 2,10,18 ? * *")).toMatchObject({ intervalMinutes: 480, label: "3×/day" });
    expect(parseSchedule("? 5 6 ? * *")).toMatchObject({ intervalMinutes: 1440, label: "daily 06:05" });
    expect(parseSchedule("? 0 */6 * * *")).toMatchObject({ intervalMinutes: 360, label: "every 6 h" });
  });
  it("null, empty string and JSON null are on demand", () => {
    expect(parseSchedule(null)).toEqual({ kind: "on_demand" });
    expect(parseSchedule("")).toEqual({ kind: "on_demand" });
  });
  it("anything else is unknown and shown verbatim", () => {
    expect(parseSchedule("? 0-59/15 * ? * *")).toEqual({ kind: "unknown", raw: "? 0-59/15 * ? * *" });
    expect(parseSchedule({ type: "everyN" })).toMatchObject({ kind: "unknown" });
    expect(parseSchedule(false)).toMatchObject({ kind: "unknown" });
  });
});

describe("stallState — against the sync time, never the wall clock", () => {
  const cron15 = `? 5,20,35,50 ${HOURS} ? * *`;
  const sync = "2026-09-02T18:12:00Z";
  it("21 minutes before the sync on a 15-minute schedule is on time", () => {
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: "2026-09-02T17:51:00Z", lastSyncedAt: sync })).toEqual({ state: "on_time", intervalMinutes: 15 });
  });
  it("3 hours before the sync is stalled? with 12 runs missed", () => {
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: "2026-09-02T15:12:00Z", lastSyncedAt: sync })).toEqual({ state: "stalled", missedRuns: 12, intervalMinutes: 15 });
  });
  it("paused is never stalled; on demand and unknown crons make no claim; no run is its own state", () => {
    expect(stallState({ schedule: cron15, disabled: true, lastExecutedAt: "2024-04-15T00:00:00Z", lastSyncedAt: sync }).state).toBe("paused");
    expect(stallState({ schedule: null, disabled: false, lastExecutedAt: "2026-01-01T00:00:00Z", lastSyncedAt: sync }).state).toBe("on_demand");
    expect(stallState({ schedule: "? 0-59/15 * ? * *", disabled: false, lastExecutedAt: "2026-01-01T00:00:00Z", lastSyncedAt: sync }).state).toBe("unknown");
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: null, lastSyncedAt: sync }).state).toBe("no_run");
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: "2026-09-02T15:12:00Z", lastSyncedAt: null }).state).toBe("unknown");
  });
});
```

Run: `cd frontend && npx vitest run src/components/celigo/__tests__/schedule.test.ts` → FAIL (module missing).

- [ ] **Step 2: schedule.ts**

```ts
import type { CeligoSchedule } from "@/hooks/use-celigo-flows";

/** Only the six-field shape `? <minutes> <hours> ? * *` seen live is parsed. Minutes:
 * a comma list or `*\/N`. Hours: a comma list, `*`, `0-23`, or `*\/N`. Anything else
 * is `unknown` and rendered verbatim -- a humaniser that guesses becomes a check that lies. */
function expand(field: string, max: number): number[] | null {
  if (field === "*" || field === `0-${max - 1}`) return Array.from({ length: max }, (_, i) => i);
  const step = /^\*\/(\d+)$/.exec(field);
  if (step) { const n = Number(step[1]); return n > 0 ? Array.from({ length: Math.ceil(max / n) }, (_, i) => i * n) : null; }
  if (!/^\d+(,\d+)*$/.test(field)) return null;
  const list = field.split(",").map(Number).filter((v) => v >= 0 && v < max);
  return list.length ? [...new Set(list)].sort((a, b) => a - b) : null;
}
function maxGap(values: number[], period: number): number {
  if (values.length === 1) return period;
  const gaps = values.slice(1).map((v, i) => v - values[i]);
  gaps.push(period - values[values.length - 1] + values[0]);
  return Math.max(...gaps);
}
export function parseSchedule(schedule: CeligoSchedule): ParsedSchedule {
  if (schedule === null || schedule === "") return { kind: "on_demand" };
  if (typeof schedule !== "string") return { kind: "unknown", raw: JSON.stringify(schedule) };
  const parts = schedule.trim().split(/\s+/);
  if (parts.length !== 6 || parts[0] !== "?" || parts[3] !== "?" || parts[4] !== "*" || parts[5] !== "*") return { kind: "unknown", raw: schedule };
  const minutes = expand(parts[1], 60); const hours = expand(parts[2], 24);
  if (!minutes || !hours) return { kind: "unknown", raw: schedule };
  const allHours = hours.length === 24;
  let intervalMinutes: number; let label: string;
  if (allHours) { intervalMinutes = maxGap(minutes, 60); label = minutes.length === 1 ? `hourly at :${String(minutes[0]).padStart(2, "0")}` : `every ${intervalMinutes} min`; }
  else if (hours.length === 1 && minutes.length === 1) { intervalMinutes = 1440; label = `daily ${String(hours[0]).padStart(2, "0")}:${String(minutes[0]).padStart(2, "0")}`; }
  else { const gapH = maxGap(hours, 24); intervalMinutes = gapH * 60; const even = hours.every((h, i) => i === 0 || h - hours[i - 1] === hours[1] - hours[0]); label = even && 24 % gapH === 0 && gapH < 8 ? `every ${gapH} h` : `${hours.length}×/day`; }
  const display = `? ${parts[1]} ${allHours ? "0…23" : parts[2]} ? * *`;
  return { kind: "cron", cron: schedule, intervalMinutes, label, display };
}
export function stallState({ schedule, disabled, lastExecutedAt, lastSyncedAt }: { schedule: CeligoSchedule; disabled: boolean | null; lastExecutedAt: string | null; lastSyncedAt: string | null }): StallState {
  if (disabled === true) return { state: "paused" };
  const parsed = parseSchedule(schedule);
  if (parsed.kind === "on_demand") return { state: "on_demand" };
  if (parsed.kind === "unknown" || parsed.intervalMinutes === null) return { state: "unknown" };
  if (!lastExecutedAt) return { state: "no_run" };
  if (!lastSyncedAt) return { state: "unknown" };
  const ageMin = (Date.parse(lastSyncedAt) - Date.parse(lastExecutedAt)) / 60000;
  if (!Number.isFinite(ageMin)) return { state: "unknown" };
  if (ageMin > 2 * parsed.intervalMinutes) return { state: "stalled", missedRuns: Math.floor(ageMin / parsed.intervalMinutes), intervalMinutes: parsed.intervalMinutes };
  return { state: "on_time", intervalMinutes: parsed.intervalMinutes };
}
```

(Type declarations as in Interfaces.) Note `every 4 h`: hours `0,4,…,20` → gap 4 → "every 4 h"; `2,10,18` → gap 8 → `3×/day`; `*/6` expands to `0,6,12,18` → "every 6 h". Run → PASS; adjust the label rule only if a test disagrees with the mockup wording (the mockup is the spec).

- [ ] **Step 3: shared.tsx + chips.ts with failing tests first**

Chips test:

```ts
// frontend/src/components/celigo/__tests__/chips.test.ts
import { describe, expect, it } from "vitest";
import { affordanceChips } from "../chips";
import type { CeligoFlowStep } from "@/hooks/use-celigo-flows";

const base: CeligoFlowStep = { id: "s", celigo_id: "c", role: "processor", kind: "destination", router_id: null, branch_id: null, branch_key: "$root", sequence: 0, adaptor_type: "NetSuiteDistributedImport", connection_celigo_id: null, filter_json: null, mapping_json: null, proceed_on_failure: null, skip_retries: null, attachments: [], reference_name: null, record_type: "salesorder", operation: "add", search_id: null, error_count: 0 };
const hook = { id: "a1", flow_id: "f", flow_step_id: "s", script_id: "scr", script_celigo_id: "scr", function_name: "preMap", json_path: "x.hooks.preMap", site_type: "hook", script_name: "ns_sales_order_premap", script_size_chars: 2443, script_copies_count: 7, script_versions_count: 3, script_version_letter: "C", script_content_diverged: true };

describe("affordanceChips — Celigo's per-side order, three states", () => {
  it("destination: input filter · NetSuite mapping (always unsynced) · response mapping · hooks", () => {
    const chips = affordanceChips({ ...base, attachments: [hook] });
    expect(chips.map((c) => `${c.slot}:${c.state}`)).toEqual(["input_filter:none", "ns_mapping:unsynced", "response_mapping:none", "hooks:configured"]);
    expect(chips[3]).toMatchObject({ label: "HK preMap", versionLetter: "C", versionsCount: 3, diverged: true });
  });
  it("source: transform · hooks · output filter; a filter_json counts as configured with its rule count", () => {
    const chips = affordanceChips({ ...base, role: "generator", kind: "source", adaptor_type: "HTTPExport", filter_json: ["and", ["equals", "a", "b"], ["equals", "c", "d"]] });
    expect(chips.map((c) => c.slot)).toEqual(["transform", "hooks", "output_filter"]);
    expect(chips[2]).toMatchObject({ state: "configured", label: "filter · 2 rules" });
  });
  it("lookup: input filter · response mapping · hooks · transform; mapping fields are counted", () => {
    const chips = affordanceChips({ ...base, kind: "lookup", adaptor_type: "HTTPExport", mapping_json: { fields: new Array(23).fill({}) } });
    expect(chips.map((c) => c.slot)).toEqual(["input_filter", "response_mapping", "hooks", "transform"]);
    expect(chips[1]).toMatchObject({ state: "configured", label: "⇄ response · 23 fields" });
  });
  it("a single-copy hook shows ×1, not a letter", () => {
    const chips = affordanceChips({ ...base, attachments: [{ ...hook, script_copies_count: 1, script_versions_count: 1, script_version_letter: null, script_content_diverged: false }] });
    expect(chips[3]).toMatchObject({ label: "HK preMap", copiesCount: 1, versionLetter: null, diverged: false });
  });
});
```

Rule count on the client: reuse the same rule (`["and"|"or", ...]` counts its members, any other non-empty list is 1, a `{rules: [...]}` object unwraps) — implement `countRules(value: CeligoJson): number` inside `chips.ts` and test the object form too. Shared tests (`shared.test.tsx`): `formatRelativeTime("2026-09-02T17:51:00Z", new Date("2026-09-02T18:12:00Z")) === "21 min ago"`, hours/days/months/years cases, `null → "—"`; `adaptorFamily("NetSuiteDistributedImport") === "NetSuite"`, `"AS2Import" → "AS2"`, `"RDBMSExport" → "RDBMS"`, `"RESTImport" → "REST"`, `"HTTPExport" → "HTTP"`, `null → null`; `fallbackStepTitle` for the four cases in Global Constraints; `deriveFlowSummary` on a detail with source + 1 lookup + router + 2 lanes returns a sentence containing "routes on" and "adds the sales order" (build the detail literal from Task 3's fixture shape); `ErrorPill` renders "0 open errors · checked 21 min ago" and "10 open · 1 root cause". Write these tests, run (FAIL), implement, run (PASS).

`deriveFlowSummary(detail)`: "Gets {records|source title} from {family} → {n lookups: 'looks each one up again'} → routes on {branch rule summary or 'N branches'} → per branch: {distinct destination verbs in sequence, e.g. 'looks up the customer, adds it, updates it, then adds the sales order'}." Fall back to "{step_count} steps · {router_count} routers" when kinds are missing.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/celigo
git commit -m "feat(celigo-ui): schedule parsing + stall heuristic, shared pills/medallions/fallbacks, affordance chips"
```

---

### Task 9: URL state — `useCeligoRoute`, surface switch, workspace wiring

**Files:**
- Create: `frontend/src/components/celigo/celigo-route.ts`, `frontend/src/components/celigo/celigo-surface.tsx`
- Modify: `frontend/src/app/(dashboard)/workspace/page.tsx` (lines ~322-323, 420-441, 509-548, 579, 770-777), `frontend/src/app/(dashboard)/workspace/surface-toggle.tsx` (no API change)
- Test: `frontend/src/components/celigo/__tests__/celigo-route.test.tsx`, `frontend/src/app/(dashboard)/workspace/__tests__/celigo-surface.test.tsx`

**Interfaces (produces):**
```ts
export type CeligoRoute = { surface: "files" | "celigo"; view: "tiles" | "list"; integrationId: string | null; tab: "flows" | "scripts" | "errors" | "changes"; flowId: string | null; stepId: string | null; scriptId: string | null };
export function readCeligoRoute(params: URLSearchParams): CeligoRoute;                       // pure
export function useCeligoRoute(): CeligoRoute & { go: { files(): void; integrations(view?: "tiles" | "list"): void; integration(id: string, tab?: CeligoRoute["tab"]): void; flow(id: string): void; step(stepId: string | null): void; script(scriptId: string | null): void } };
export function CeligoSurface(): JSX.Element;      // switches on the route: integrations | integration | flow
export function CeligoBreadcrumb(props: { items: { label: string; onClick?: () => void }[] }): JSX.Element;
```
Rules: `go.files()` = push `/workspace` with every celigo param removed; `go.integrations()` = push with `surface=celigo` (+`view=list` only when list) and `file`/`workspace` removed; `integration`/`flow` push; `step`/`script` use `router.replace`; unknown `view`/`tab` values normalise to defaults.

- [ ] **Step 1: Failing route tests**

```tsx
// frontend/src/components/celigo/__tests__/celigo-route.test.tsx
import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: nav.push, replace: nav.replace }), useSearchParams: () => nav.params, usePathname: () => "/workspace" }));

import { readCeligoRoute, useCeligoRoute } from "../celigo-route";

beforeEach(() => { nav.push.mockReset(); nav.replace.mockReset(); nav.params = new URLSearchParams(); });

describe("readCeligoRoute", () => {
  it("defaults to files and normalises bad values", () => {
    expect(readCeligoRoute(new URLSearchParams("")).surface).toBe("files");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&view=bogus&tab=bogus")).view).toBe("tiles");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&integration=i1&tab=scripts")).tab).toBe("scripts");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&flow=f1&step=s1&script=x1"))).toMatchObject({ flowId: "f1", stepId: "s1", scriptId: "x1" });
  });
});

describe("useCeligoRoute is the only writer", () => {
  it("entering the surface drops file/workspace params; leaving drops celigo params", () => {
    nav.params = new URLSearchParams("file=a.js&workspace=w1");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.integrations());
    expect(nav.push).toHaveBeenCalledWith("/workspace?surface=celigo");
    nav.params = new URLSearchParams("surface=celigo&flow=f1&step=s1");
    const r2 = renderHook(() => useCeligoRoute());
    act(() => r2.result.current.go.files());
    expect(nav.push).toHaveBeenLastCalledWith("/workspace");
  });
  it("levels push, selection replaces", () => {
    nav.params = new URLSearchParams("surface=celigo&flow=f1");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.step("s9"));
    expect(nav.replace).toHaveBeenCalledWith("/workspace?surface=celigo&flow=f1&step=s9");
    act(() => result.current.go.integration("i1", "errors"));
    expect(nav.push).toHaveBeenCalledWith("/workspace?surface=celigo&integration=i1&tab=errors");
    act(() => result.current.go.flow("f2"));
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&flow=f2");
  });
});
```

Run → FAIL.

- [ ] **Step 2: Implement `celigo-route.ts`**

Build URLs with `new URLSearchParams()` in a fixed key order (`surface, view, integration, tab, flow, step, script`) so tests are stable; `go.step(null)` removes `step` and `script`; `go.script(x)` keeps `flow`+`step`. Run → PASS.

- [ ] **Step 3: Failing page-level test, then wire the workspace page**

Rewrite `frontend/src/app/(dashboard)/workspace/__tests__/celigo-surface.test.tsx` (keep its toggle cases) and add:

```tsx
it("with the flag on and ?surface=celigo the Celigo surface is mounted and no deploy affordance is in the tree", async () => {
  nav.params = new URLSearchParams("surface=celigo");
  features.mockReturnValue(true);
  render(wrap(<WorkspacePage />));
  expect(await screen.findByTestId("celigo-surface")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /deploy/i })).toBeNull();
  expect(screen.queryByText(/changeset/i)).toBeNull();
});
it("with the flag off ?surface=celigo renders Files", () => {
  nav.params = new URLSearchParams("surface=celigo");
  features.mockReturnValue(false);
  render(wrap(<WorkspacePage />));
  expect(screen.queryByTestId("celigo-surface")).toBeNull();
});
```

Mock `@/hooks/use-features` (`useFeature: () => features()`), `@/components/celigo/celigo-surface` to `() => <div data-testid="celigo-surface" />` for these page tests, and the workspace hooks the page already needs (copy the mocks from `frontend/src/app/(dashboard)/workspace/__tests__/*.test.tsx` that already render the page, if any; otherwise mock `@/hooks/use-workspace*` modules the page imports — read the page's imports first). Run → FAIL. Then in `page.tsx`:
- replace `useState<WorkspaceSurface>("files")` with `const route = useCeligoRoute(); const surface = showCeligo ? route.surface : "files";`
- `<CeligoSurfaceToggle surface={surface} onChange={(s) => (s === "celigo" ? route.go.integrations() : route.go.files())} enabled={showCeligo} />`
- the `file`/`workspace` effect: `if (route.surface === "celigo") return;` first line.
- keyboard effect: when `surface === "celigo"`, ⌘K and ⌘B call `window.dispatchEvent(new CustomEvent("celigo:command-k"))` / `("celigo:toggle-nav")` and return (the Celigo pages listen; Tasks 11 and 14), never touch `fileTreeRef`/`searchInputRef`.
- the Celigo branch renders `<div className="flex-1 min-h-0 flex flex-col" data-testid="celigo-surface-host"><CeligoSurface /></div>` instead of `<CeligoFlowMap />` (keep the import of `CeligoFlowMap` until Task 18 deletes the file; the branch must no longer reference it).
Create `celigo-surface.tsx` with the switch rendering placeholders `<CeligoIntegrationsPage/>` etc. — for this task, three stub components in the same file exporting the names Tasks 10/12/14 will replace, each rendering its breadcrumb and a "Loading…" line. Run → PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/celigo frontend/src/app/\(dashboard\)/workspace
git commit -m "feat(celigo-ui): URL-driven Celigo surface with a single route writer; workspace page reads surface from the URL"
```

---

### Task 10: Integrations page (tiles, list, filters, sort)

**Files:**
- Create: `frontend/src/components/celigo/celigo-integrations-page.tsx`
- Modify: `frontend/src/components/celigo/celigo-surface.tsx` (replace the stub)
- Test: `frontend/src/components/celigo/__tests__/celigo-integrations-page.test.tsx`

**Interfaces:**
- Consumes: `useCeligoIntegrations()` (Task 6 shape), `useCeligoSyncStatus()`, `stallState`, `parseSchedule`, `ErrorPill`, `SchedulePill`, `Medallions`, `formatRelativeTime`, `useCeligoRoute().go`.
- Produces: `CeligoIntegrationsPage()`; pure `integrationAttention(i, lastSyncedAt): { stalledCount: number; allPaused: boolean; onDemandOnly: boolean }` and `sortIntegrations(list, lastSyncedAt)` exported for tests.

- [ ] **Step 1: Failing tests** (mock `@/hooks/use-celigo-flows` like the existing suite; mock `next/navigation` as in Task 9; use `resolved()/pending()/errored()` helpers copied into a new `frontend/src/components/celigo/__tests__/query-fixtures.ts` and imported by every Celigo test from now on)

Cases:
1. Two integrations resolved + sync status resolved → each tile shows name, `"20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps"`, writes `salesorder ×19`, `0 open errors · checked 21 min ago`, medallions `NS`/`HTTP`; header line `"2 integrations · 27 flows · … · production only"`.
2. Attention-first sort: an integration whose `flow_schedules` contains a stalled flow (cron 15 min, last run 3 h before sync) sorts before a bigger quiet one; the stalled one shows `stalled? 1 flow`.
3. An integration with `flow_count == paused_count` renders dimmed (`data-state="all-paused"`) with pill "all paused"; one with `scheduled_count === 0` and some on-demand shows "on demand only".
4. Filters: clicking "Stalled" leaves only the stalled tile; "Open errors" filters `error_count > 0`.
5. List toggle ☰ calls `go.integrations("list")`; with `?view=list` the table renders rows with the same counts.
6. `useCeligoIntegrations()` pending → skeleton, never "No integrations"; errored → `ErrorNotice` with Retry calling `refetch`; resolved `[]` with sync status `{last_synced_at: null}` → "No flows synced yet — run a sync from the connector card in Settings." and counts "—".
7. Clicking a tile calls `go.integration(id)`.

- [ ] **Step 2: Implement**

Tile = `rounded-xl border bg-card p-4 shadow-soft flex flex-col gap-1.5` exactly as the mockup: row 1 pills + medallions; `<h4 className="text-[15px] font-semibold">`; counts line `text-[12px] text-muted-foreground`; writes line (`writes` uppercase label + `font-mono` chips, `+N custom records` when more than 5 types, "no NetSuite writes" when empty); foot `last run · N scripts · changes N` (`changes_last_24h`). Sort: `error_count desc, stalledCount desc, flow_count desc, name`. Filters row `All N · Open errors N · Stalled N · All paused N` (`aria-pressed`). Grid `grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(290px,1fr))]`. Page header stats from the sums. Sync pill in the page header (`synced {rel} · {HH:MM} UTC`, amber past 2 h). Run tests → PASS. `npx tsc --noEmit`, `npx next lint`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo
git commit -m "feat(celigo-ui): My integrations — dense tiles, list view, attention-first sort, filters"
```

---

### Task 11: Command palette (⌘K) over integrations and flows

**Files:**
- Create: `frontend/src/components/celigo/celigo-command-palette.tsx`
- Modify: `frontend/src/components/celigo/celigo-surface.tsx` (mount once)
- Test: `frontend/src/components/celigo/__tests__/celigo-command-palette.test.tsx`

**Interfaces:**
- Consumes: `cmdk` (`Command`, `Command.Dialog` is not used — render `Command` inside the existing Radix `Dialog` from `@/components/ui/dialog` for consistent styling); `useCeligoIntegrations()` (`flow_schedules` gives every flow without extra requests); `stallState`; `useCeligoRoute().go`; the `celigo:command-k` window event from Task 9.
- Produces: `CeligoCommandPalette()`.

- [ ] **Step 1: Failing tests**

1. Dispatching `new CustomEvent("celigo:command-k")` opens the dialog (`role="dialog"`, input `placeholder="Search integrations & flows"`).
2. Typing "Multi" filters to the flow "New Sales Order to NetSuite - Multi-Subsidiary" showing its integration name and a schedule/stall dot (`data-state="on_time"`); Enter calls `go.flow("f1")`.
3. An integration result calls `go.integration(id)`.
4. Escape closes. Names only: the rendered list never contains `content` (assert no `<pre>` and that a decoy string put into a step's script name is fine but nothing else).

- [ ] **Step 2: Implement** — `Command` with `Command.Input`, `Command.List`, two `Command.Group`s ("Integrations", "Flows"); each item `value={`${kind}:${name}`}`; row layout: health dot (colour by `stallState`: ok/warn/mute) · name · muted integration name. Run → PASS, lint, tsc.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo
git commit -m "feat(celigo-ui): ⌘K palette over integrations and flows (names only)"
```

---

### Task 12: Integration page (header, tabs, flows table, step-errors drawer)

**Files:**
- Create: `frontend/src/components/celigo/celigo-integration-page.tsx`
- Modify: `frontend/src/components/celigo/celigo-surface.tsx`
- Test: `frontend/src/components/celigo/__tests__/celigo-integration-page.test.tsx`

**Interfaces:**
- Consumes: `useCeligoIntegrations()` (to find the integration by id + its summary), `useCeligoIntegrationFlows(id)` (Task 5 shape), `useCeligoSyncStatus()`, `useCeligoFlowErrors(flowId)` (for the errors drawer), `useCeligoIntegrationChanges(id)`, schedule + pills, `go`.
- Produces: `CeligoIntegrationPage()`; pure `groupFlows(flows): { key: "scheduled" | "on_demand" | "paused"; label: string; flows: CeligoFlowSummary[] }[]` and `topologyGlyph(flow): { routers: number; steps: number }` exported for tests.

- [ ] **Step 1: Failing tests**

1. Header shows name, `Production`, medallions, `"20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps · 11 routers · 24 lookups · 30 scripts"`, writes line; tabs `Flows 20 · Scripts 30 · Errors N · Changes N`; breadcrumb "My integrations › Solidus + NetSuite" and clicking "My integrations" calls `go.integrations()`.
2. Table groups: rows under "On · scheduled", "On · on demand", "Paused in Celigo" in that order; paused rows have `data-paused="true"` and State "Paused"; a cron row shows `every 15 min` plus the verbatim display `? 5,20,35,50 0…23 ? * *`; an unknown cron shows the raw string only; on-demand rows show "on demand".
3. Steps glyph: a flow with `router_count 2, step_count 10` renders `◉→◇◇→10`; no routers `◉→3`.
4. Last run relative to sync + pill: `21 min ago` + `on time`; stalled example → `stalled? 12 runs missed`; `last_executed_at null` → `no run recorded`.
5. Scripts cell `2 · 1 diverged`; Errors cell shows `signature_count` leading (`1 root cause · 10`) when > 0 else `0`; clicking a non-zero errors count opens a drawer (`role="dialog"`, title "Flow: {name}") listing `step_ids` with counts from `useCeligoFlowErrors`.
6. Row click → `go.flow(id)`. Tabs write `go.integration(id, "changes")`; Changes tab lists change rows `field · old → new · relative time`, empty state "No configuration changes recorded since syncing began."; Scripts tab renders "30 scripts across 20 flows · the Scripts view ships separately" plus the flows with `script_count > 0`; Errors tab lists flows with `error_count > 0` or "No open errors. Celigo reported 0 on the last sync, 21 min ago."
7. Unknown integration id (not in the list) → "This integration is not in the last sync." with a link calling `go.integrations()`; flows query errored → `ErrorNotice`; pending → skeleton rows, never "0 flows".

- [ ] **Step 2: Implement** with `@/components/ui/table` and `@/components/ui/tabs`; drawer = Radix `Dialog` positioned right (`fixed inset-y-0 right-0 w-[520px]`). Run → PASS, lint, tsc.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo
git commit -m "feat(celigo-ui): integration page — grouped flows table with topology glyph, schedule and stall pills, tabs"
```

---

### Task 13: Deterministic layered layout (pure)

**Files:**
- Create: `frontend/src/components/celigo/layout.ts`
- Test: `frontend/src/components/celigo/__tests__/layout.test.ts`

**Interfaces (produces):**
```ts
export const BUBBLE_W = 212, BUBBLE_H = 162, ROUTER_W = 110, ROUTER_H = 56, GAP_X = 22, LANE_PITCH = 252, MARGIN = 20, LANE_LABEL_H = 24;
export type LayoutNode = { id: string; type: "step" | "router" | "placeholder"; x: number; y: number; w: number; h: number; stepId?: string; routerId?: string; branchId?: string | null; lane?: number };
export type LayoutEdge = { id: string; from: string; to: string; x1: number; y1: number; x2: number; y2: number; curved: boolean; dashed: boolean; label?: string };
export type LaneLabel = { routerId: string; branchId: string; name: string | null; ruleCount: number; order: number; x: number; y: number };
export type Layout = { nodes: LayoutNode[]; edges: LayoutEdge[]; lanes: LaneLabel[]; captions: { x: number; y: number; text: string }[]; width: number; height: number; warnings: string[] };
export function computeLayout(detail: Pick<CeligoFlowDetail, "steps" | "routers">): Layout;
```
Algorithm (ranks are x columns, `x = MARGIN + Σ(prev widths + GAP_X)`):
1. **Sources** (`role === "generator"`, by `sequence`) stacked vertically in rank 0.
2. **Top-level chain** (`role === "processor" && router_id === null`, by `sequence`): one rank each.
3. **Routers**: take `detail.routers` in declared order; add synthetic routers for any `router_id` on steps not declared (`name: null`, branches = distinct `branch_id`s lexical, `warnings.push("router order unverified")`). Build the chain: a router is *pass-through* when it has exactly one branch whose `next_router_id` is another router; the spine continues: router rank → that branch's steps as ranks on the spine row → the next router. The first router with >1 branch (or whose single branch has no `next_router_id`) is the *fan-out* router: each of its branches becomes a lane (rows, in declared order, `y = MARGIN + LANE_LABEL_H + lane*LANE_PITCH`), its steps (by `sequence`) laid out as consecutive ranks right of the router. A branch whose `next_router_id` is set inside a lane gets that router appended at the end of the lane (`warnings.push("nested router chain drawn inline")`), its own branches NOT expanded (placeholder node "N branches · open in Celigo").
4. Any remaining undeclared/unchained routers are stacked below the last lane as extra fan-out blocks with `warnings.push("router order unverified")`.
5. Spine y = vertical centre of the lanes' bounding box (or `MARGIN + LANE_LABEL_H` when no lanes); sources stack around it.
6. Empty declared branch → `placeholder` node (bubble size, `id: "placeholder:<branchId>"`).
7. Edges: last source → first spine node; spine node i → i+1; fan-out router → first node of each lane (`curved: true`); lane node i → i+1; `dashed`/`label: "continues on failure"` when the FROM step has `proceed_on_failure === true`. Edge endpoints: from `(x+w, y+h/2)` to `(x, y+h/2)`.
8. `width = max(x+w) + MARGIN`, `height = max(y+h) + MARGIN`; 0 steps → `nodes: []`, `warnings: ["no steps recorded"]`, `width/height = 0`.
Node ids: steps `step.id`; routers `router:<id>`.

- [ ] **Step 1: Failing fixture-matrix tests** — build a `mk(steps, routers)` helper and assert for each: (a) single source→destination: 2 nodes, 1 edge, same y; (b) two sources: stacked, x equal, the chain edge from the LAST source; (c) top-level chain of 3: x strictly increasing, 2 edges; (d) one router with 2 lanes of 2: router node present, lanes at `LANE_PITCH` apart in declared order ("Framework Intl" first even though branch id sorts after), 2 curved edges from the router, lane labels carry names and rule counts; (e) empty declared lane → placeholder node; (f) duplicate `celigo_id` in two branches → two step nodes; (g) 0 steps → empty + warning; (h) the Multi-Subsidiary chain (from Task 3's fixture shape: `r1` pass-through with the lookup, `r2` fan-out) → node order along the spine is source, router:r1, lookup, router:r2 with increasing x, then 2 lanes of 4; (i) two undeclared routers (steps only) → both drawn, warning "router order unverified"; (j) `proceed_on_failure` → dashed edge with label. Run → FAIL.

- [ ] **Step 2: Implement `computeLayout`** per the algorithm. Run → PASS.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo/layout.ts frontend/src/components/celigo/__tests__/layout.test.ts
git commit -m "feat(celigo-ui): deterministic layered layout for sources, router chains and branch lanes"
```

---

### Task 14: Flow page shell — panel group, header, navigator, inspector frame, states

**Files:**
- Create: `frontend/src/components/celigo/celigo-flow-page.tsx`, `celigo-flow-header.tsx`, `celigo-flow-navigator.tsx`
- Modify: `frontend/src/components/celigo/celigo-surface.tsx`
- Test: `frontend/src/components/celigo/__tests__/celigo-flow-page.test.tsx`

**Interfaces:**
- Consumes: `useCeligoFlowDetail(flowId)`, `useCeligoIntegrationFlows(detail.integration_id)` (navigator + sibling hop), `useCeligoIntegrations()` (integration name for the breadcrumb, `flow_schedules` for the navigator dots), `useCeligoSyncStatus()`, `deriveFlowSummary`, pills, `useCeligoRoute()`; react-resizable-panels `Group`/`Panel`/`Separator` (`orientation="horizontal"`), `PanelImperativeHandle`.
- Produces: `CeligoFlowPage()`; `CeligoFlowHeader({ detail, lastSyncedAt, integrationName })`; `CeligoFlowNavigator({ flows, currentFlowId, lastSyncedAt, collapsed, onToggle, onSelect })`; the page renders a `CeligoFlowCanvas` slot (Task 15) and `CeligoStepInspector` slot (Task 16) — for this task both are stub components exported from their future files with the final prop contracts:
  - `CeligoFlowCanvas({ detail, selectedStepId, onSelectStep(stepId: string | null, tab?: InspectorTab), paused })`
  - `CeligoStepInspector({ detail, step: CeligoFlowStep | null, tab, onTabChange, lastSyncedAt, onOpenScript(scriptId) })` where `type InspectorTab = "facts" | "filter" | "mapping" | "scripts" | "errors"`.

- [ ] **Step 1: Failing tests**

1. Header: pills `0 open errors · checked 21 min ago` + `on time`; name; facts line contains `every 15 min`, `? 5,20,35,50 0…23 ? * *`, `America/Los_Angeles`, `last ran 17:51 UTC · 21 min before the sync`, `10 steps · 2 routers · 3 branches · 3 lookups`, `writes salesorder ×2 · customer ×4`, `2 scripts`, `1 diverged family`, `modified in Celigo 2 Sep 2026`; `cloned from a flow no longer in the account` when `source_id` is set but no sibling flow has that `celigo_id`, or `cloned from {name}` when one does; derived summary line present; AI description block with caption `inherited from the clone source` when `source_id` is set; the "Open in Celigo ↗" anchor has `href="https://integrator.io/integrations/{integration celigo_id}/flowBuilder/{flow celigo_id}"`, `target="_blank"`, `rel="noreferrer"`; "Copy link" writes `location.href` to `navigator.clipboard.writeText` (mock).
2. Panel group has `id="celigo-flow-v1"`; the navigator starts collapsed as a rail (`data-testid="celigo-nav-rail"`) with one dot per sibling flow (`data-state` = stall state, current one `data-current="true"`); dispatching `celigo:toggle-nav` expands to the list; clicking a sibling calls `go.flow(id)`.
3. Paused flow: banner text exactly "This flow is Off in Celigo — mirrored here, not changeable here." and the canvas host has `data-paused="true"`.
4. `steps: []` → sentence "No steps recorded for this flow in the last sync." in the canvas slot.
5. Detail errored → header keeps the breadcrumb, canvas slot shows `ErrorNotice("Couldn't load this flow.")` with Retry; detail 404 (`isError` with status 404 — mock `error: { status: 404 }`) → "This flow is not in the last sync." + link to the integration page.
6. Escape with a selected step clears it (`go.step(null)`); a second Escape with a script open closes it (`go.script(null)`) — assert call order with the route mock.
7. Inspector resting state (no step selected): the inspector slot receives `step: null` and the page renders the Overview (`data-testid="celigo-overview"`) with the AI description and sync freshness.

- [ ] **Step 2: Implement** — `PanelGroup id="celigo-flow-v1" orientation="horizontal"`; Panels: navigator (`defaultSize={16}`, `collapsible`, `panelRef={navRef}`, collapsed = rail), canvas (flex), inspector (`defaultSize={24}`, `minSize={20}`). Header per the mockup classes. Keyboard: a `useEffect` listening to `keydown` Escape and the two custom events. Run → PASS, lint, tsc.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo
git commit -m "feat(celigo-ui): full-page flow shell — header facts, navigator rail, inspector frame, paused/empty/failed/unknown states"
```

---

### Task 15: Canvas — bubbles, routers, lanes, edges, fit/zoom, selection

**Files:**
- Create: `frontend/src/components/celigo/celigo-flow-canvas.tsx`, `step-bubble.tsx`, `router-node.tsx`
- Test: `frontend/src/components/celigo/__tests__/celigo-flow-canvas.test.tsx`, `__tests__/step-bubble.test.tsx`

**Interfaces:**
- Consumes: `computeLayout`, `affordanceChips`, `fallbackStepTitle`, `adaptorFamily`, `CeligoFlowCanvas` prop contract from Task 14.
- Produces: `StepBubble({ step, node, selected, paused, onSelect(stepId, tab?) })`, `RouterNode({ router, node, index })`.

- [ ] **Step 1: Failing bubble tests**

1. Eyebrow `Lookup · HTTP` with app glyph `H`; title = `reference_name` when set, else the fallback with `data-unsynced="true"`; fact line `import · add · salesorder` / `export · saved search 5090 · customer` / `http export · conn 648bd44c…` (first 8 chars of `connection_celigo_id` + `…`).
2. Chips render in the order and states from `affordanceChips`; a hook chip shows `HK preMap` + `C/3` badge + amber dot when diverged; single copy shows `×1`; clicking a chip calls `onSelect(step.id, "scripts")` (hooks/transform), `"filter"`, or `"mapping"`.
3. Footer: `stops flow on failure` when `proceed_on_failure === false`, `continues on failure` (amber) when true, `stops on failure · default` when null (destinations); `retries on` / `retries skipped` for sources.
4. `error_count > 0` → red badge `10 open` in the eyebrow and `data-error="true"`.
5. `selected` → `data-selected="true"`; `paused` → opacity class `opacity-60`.

Canvas tests: renders one bubble per step (10 for the Multi-Subsidiary fixture) and two router nodes with `Router 1 · pass-through · 1 branch → chains to router 2` / `Router 2 · first matching branch · by input filters · 2`; lane labels `Branch 1 · Framework Intl · 1 rule`; SVG `path` count equals `layout.edges.length`; a dashed edge has `stroke-dasharray`; clicking a bubble calls `onSelectStep(id)`; the zoom label reads `fit · NN%` and the `+` button switches to `100%` (mock `clientWidth` via `Object.defineProperty` on the wrap ref); warnings render as an amber caption; legend row present with the three chip states.

- [ ] **Step 2: Implement** — the canvas is the mockup's DOM: a dotted ground (`bg-[radial-gradient(var(--dot)_1px,transparent_1px)] [background-size:18px_18px]` via a small CSS module or inline style), a sizer div sized to `scale × layout` with `overflow:hidden`, a scaled inner div (`transform: scale(s); transform-origin: 0 0`) holding the absolutely positioned nodes and one `<svg>` with `<marker>` arrowheads; `ResizeObserver` (already stubbed in vitest setup) recomputes fit; fit floor 0.6. Run → PASS, lint, tsc.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo
git commit -m "feat(celigo-ui): flow canvas — step bubbles with affordance chips, router nodes, branch lanes, edges, fit/zoom"
```

---

### Task 16: Inspector tabs

**Files:**
- Create: `frontend/src/components/celigo/celigo-step-inspector.tsx`, `frontend/src/components/celigo/inspector-panels.tsx` (move `KeyValueOrJson`, `FilterPanel`, `FieldMappingPanel` out of `celigo-flow-map.tsx`, exported, unchanged behaviour; `FieldMappingPanel`'s heading becomes "Response mapping · N fields" and gains a muted line "NetSuite field mapping · not synced")
- Test: `frontend/src/components/celigo/__tests__/celigo-step-inspector.test.tsx`

**Interfaces:**
- Consumes: `useCeligoFlowErrors(detail.id)` (filter groups by `step_ids` containing the step), `affordanceChips`, prop contract from Task 14.

- [ ] **Step 1: Failing tests** — Facts tab: adaptor, connection (`648bd44c… · name not synced`), role line `lookup · router 1 · branch 1 · step 1 of 1` (index within its branch by `sequence`), celigo id, "used elsewhere: only in this flow" (count of steps with the same `celigo_id` in this flow's other branches → "also in Branch 2" when > 1); Filter tab renders `FilterPanel` or "No filter on this step"; Mapping tab renders the response mapping with the not-synced line; Scripts tab lists attachments as site cards: chip, script name, `1 copy · 1 version` / `copy C of 3 versions · 7 copies`, size `33.3 KB`, `json_path` verbatim, "Open source →" calling `onOpenScript(script_id)` (absent when `script_id` is null, replaced by "script body not synced"), shield copy from Global Constraints; Errors tab: quiet sentence with the sync time when no group touches this step, else signature cards (`code`, `source`, `×count`, first/last, `not retriable`, `purges 16 Sep`, trace keys as chips, "Open the N in recon →" link to `/reconciliation`); a `tab` prop of `"errors"` selects that tab and `onTabChange` fires on click. No script `content` ever renders here (assert `queryByText(/function /)` is null even when the mocked attachment's script has content).

- [ ] **Step 2: Implement** with `@/components/ui/tabs`. Run → PASS, lint, tsc.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo frontend/src/components/settings/celigo-flow-map.tsx
git commit -m "feat(celigo-ui): step inspector — facts, filter, response mapping, scripts with family state, per-step errors"
```

---

### Task 17: Script drawer (re-homed viewer, N2 copy, `&script=`)

**Files:**
- Create: `frontend/src/components/celigo/celigo-script-drawer.tsx`
- Modify: `frontend/src/components/settings/celigo-script-viewer.tsx` (extract `CeligoScriptViewerBody({ script, currentStepId? })` + `UntrustedContentBanner` with the new copy; the Dialog wrapper stays for Task 18 to delete)
- Modify: `frontend/src/components/celigo/celigo-flow-page.tsx` (mount the drawer when `route.scriptId`)
- Test: `frontend/src/components/celigo/__tests__/celigo-script-drawer.test.tsx`, update `frontend/src/components/settings/__tests__/celigo-script-viewer.test.tsx` for the new banner copy

**Interfaces:**
- Consumes: `useCeligoScript(scriptId)`, `useCeligoRoute().go.script(null)`.
- Produces: `CeligoScriptDrawer({ scriptId, onClose, returnFocusTo?: React.RefObject<HTMLElement> })`; `CeligoScriptViewerBody`.

- [ ] **Step 1: Failing tests** — with `?script=scr` the drawer renders (`role="dialog"`, `aria-label="Script source"`) as a right panel over the inspector: header `HK preMap · ns_sales_order_premap`, pills `7 copies · diverged`, `4 sites · 2 flows` (from `used_by`), size + modified; the body is the syntax highlighter with the `content` text; banner text equals the N2 copy exactly; "Scripts view ↗" link; used-by rows; Escape calls `onClose`; when `scriptId` is null nothing renders and no `content` text is in the document (the flow page test from Task 14 asserts this too with a step whose attachment script has content in the mocked `useCeligoScript`). Pending → skeleton; errored → `ErrorNotice`.

- [ ] **Step 2: Implement** — Radix `Dialog` with `DialogContent` overridden to `fixed inset-y-0 right-0 h-full w-[560px] max-w-[95vw] translate-x-0 translate-y-0 rounded-none border-l`; `onOpenAutoFocus` focuses the close button; `onCloseAutoFocus` returns focus to `returnFocusTo`. Run → PASS, lint, tsc.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/celigo frontend/src/components/settings
git commit -m "feat(celigo-ui): script drawer over the inspector; N2 banner copy"
```

---

### Task 18: Retire the old map, cross-surface consistency, e2e seed + spec

**Files:**
- Delete: `frontend/src/components/settings/celigo-flow-map.tsx`, `frontend/src/components/settings/__tests__/celigo-flow-map.test.tsx`
- Modify: `frontend/src/components/settings/celigo-script-viewer.tsx` (delete the Dialog wrapper if nothing imports it; keep `CeligoScriptViewerBody`), `frontend/src/app/(dashboard)/workspace/page.tsx` (drop the `CeligoFlowMap` import), any `formatSchedule` importer → `parseSchedule(...).label`
- Create: `frontend/src/components/celigo/__tests__/cross-surface-counts.test.tsx`, `backend/scripts/seed_celigo_e2e.py`, `frontend/e2e/celigo-flow-pages.spec.ts`
- Test: whole frontend suite

- [ ] **Step 1: Cross-surface test (failing if any surface disagrees)** — one mocked dataset (integration with `error_count: 10`, a flow summary with `error_count: 10, signature_count: 1`, a detail whose lookup step has `error_count: 10`, `flow_schedules` containing that flow, `useCeligoFlowErrors` returning one group of 10 attributed to the lookup step) rendered through `CeligoIntegrationsPage`, `CeligoIntegrationPage`, `CeligoFlowPage` (with route params set per render): assert the tile pill text `10 open · 1 root cause`, the table Errors cell, the header pill, the bubble badge `10 open`, the navigator dot `data-state="crit"` and the Errors tab count all show 10. Run → PASS (or fix the surface that disagrees).

- [ ] **Step 2: Delete the old map** — remove the files, fix imports, run `npx vitest run` (whole suite; the pre-existing `d3-force` failure in `memory-graph-canvas` is unrelated — note it in the commit body), `npx tsc --noEmit`, `npx next lint`.

- [ ] **Step 3: e2e seed script + spec**

`backend/scripts/seed_celigo_e2e.py`: idempotent; args `--tenant-slug`, `--database-url`; refuses any slug other than `uat-smoke` or one starting with `e2e-` (the recon smoke's guard pattern); enables the `celigo` flag via `enable_feature_flag` semantics (write the same row the API's feature service reads); inserts a `connections` row for provider `celigo` with an encrypted placeholder credential via `encrypt_credentials`, then the Task 3 fixture world (integration, the Multi-Subsidiary flow with routers/branches/steps/scripts/attachments, one signature with 10 open errors on the lookup step, a paused flow, an on-demand flow) using the ORM in one transaction; prints the ids. `frontend/e2e/celigo-flow-pages.spec.ts`: `test.skip(!process.env.CELIGO_E2E, "needs a seeded tenant")`; logs in with `E2E_EMAIL`/`E2E_PASSWORD`, visits `/workspace?surface=celigo`, asserts the tile pill `10 open · 1 root cause`, clicks through to the flow page, asserts 10 bubbles, `Framework Intl` lane label, the header pill, opens the lookup bubble's Errors tab and asserts the count, opens the hook chip → drawer shows the N2 banner, Escape closes. Document the local run in the spec header (`docker compose up`, seed script, `CELIGO_E2E=1 BASE_URL=http://localhost:3002 npx playwright test e2e/celigo-flow-pages.spec.ts`). CI wiring is decided in the gate phase after reading `.github/workflows` (not in this task).

- [ ] **Step 4: Commit**

```bash
git add -A frontend/src frontend/e2e backend/scripts/seed_celigo_e2e.py
git commit -m "feat(celigo-ui): retire the old flow map; cross-surface error-count consistency test; e2e seed + spec"
```

---

### Task 19: Full verification before the gate

- [ ] `./scripts/verify.sh --full` from the worktree root (it compares the suite against `origin/main` by test id; read its `baseline:` line — a PASS with no baseline is not a pass). Fix anything red.
- [ ] `cd frontend && npx vitest run && npx tsc --noEmit && npx next lint`.
- [ ] Manual check on the local stack: `docker compose up -d --build backend` in the main checkout is NOT the worktree — instead run the worktree backend against the local DB (`cd backend && /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m uvicorn app.main:app --port 8010`) and the worktree frontend (`cd frontend && NEXT_PUBLIC_API_URL=http://localhost:8010 npx next dev -p 3010`), seed with `seed_celigo_e2e.py --tenant-slug e2e-celigo`, and walk the three levels once. Screenshot each level to `docs/superpowers/mockups/verification/` is NOT required; the gate's live smoke on staging is the acceptance.
- [ ] Commit any fixes; then hand off to the gate (`Workflow({name: "code-review-multiangle", args: {target: "feat/celigo-flow-pages"}})`).

---

## Self-review

**Spec coverage.** §3.1 → Task 1. §3.2 → Tasks 3, 4 (per-step counts). §3.3 → Task 5. §3.4 → Task 6 (`flow_schedules` added so stall and the palette need no fan-out; `last_synced_at` stays on `/sync-status`). §3.5 → Tasks 4, 7. §4.1 → Task 9. §4.2 integrations → Task 10 (+11 palette); integration page → Task 12; flow page → Task 14; canvas + bubble → Tasks 13, 15; inspector → Task 16; drawer → Task 17; states → Tasks 10, 12, 14. §4.3 → Task 8 `fallbackStepTitle`. §5 → each task's tests; cross-surface + e2e → Task 18; gate → Task 19 + the ship task. Fail-closed response handling (review-gate carry-over) → Task 2. Not covered by design: connections sync, NetSuite field mapping, run history — out of scope per spec §6.

**Placeholder scan.** Every code step carries code or an exact contract; UI tasks specify DOM, copy and data attributes that the tests assert. No "TBD".

**Type consistency.** `CeligoFlowStep.kind`, `reference_name`, `record_type`, `operation`, `search_id`, `error_count` (Tasks 1, 3, 4) match the fixtures in Tasks 8, 13, 15. `CeligoRouter`/`CeligoRouterBranch` (Task 3) match `computeLayout`'s reads (`branches[].order`, `next_router_id`, `rule_count`, `name`). `InspectorTab` is defined once in Task 14 and reused in 15–16. `useCeligoFlowErrors` (Task 4) is consumed in 12, 16, 18. `go.*` names (Task 9) are used identically in 10–17.
