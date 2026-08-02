# Dashboard wallpaper — published set + per-user active dashboard + switcher

**Approved design**: artifact `wallpaper-mock-v1` — https://claude.ai/code/artifact/a3b80b53-637c-4f5c-878a-aaa3b7c92aaa (operator-approved verbatim 2026-07-24: "The mock is so good"). The rendered UI is the acceptance gate — it must match the mock, not merely pass tests.

**Ticket**: ClickUp 86bb3da28. **Tier**: T2 (Alembic migration is an explicit T2 trigger). **Branch**: `feat/dashboard-wallpaper` off `c68c024`.

## Concept

PR #181 shipped a *bulletin board*: pinned reports stacked as 300px-cropped preview cards. The operator wanted a *wallpaper*: the landing page shows **ONE** published report at full size, and each user switches which one via a `Switch ▾` menu.

- The **published set** is workspace-wide (what is *available* to display) — this is exactly today's `reports.dashboard_pinned_at`, renamed in the UI from "Pin to dashboard" to **"Publish to dashboard"**.
- The **active choice is per user** — my switch never changes anyone else's wall.
- Fallback when a user has no selection (new teammate, or their pick was deleted/unpublished): the **most recently published** report, with a one-time notice when it is a fallback after their pick vanished.

## Global Constraints (binding for every task)

1. **TDD strictly** — failing test first, then implement.
2. **Do NOT touch** `backend/app/services/report/report_html.py`, `statement_builder.py`, `playbooks.py`, `refresh_service.py` — the artifact and its byte-stability pins are out of scope.
3. **Every mutation audit-logged** via `audit_service.log_event(...)` before `await db.commit()`.
4. **No DB access at all after `db.commit()`** on an RLS-enforced table — the COMMIT clears the `SET LOCAL` tenant GUC. A `db.refresh()` 500s (caught in PR #181's final review); a plain SELECT is worse — the policy becomes `tenant_id = NULL` and it **silently returns zero rows** (found in Task 2). Order is `flush → refresh → commit`, or build the response from data loaded pre-commit. The test harness cannot see either failure (savepoints + BYPASSRLS role).
5. **Tenant scoping is explicit** — every query filters `tenant_id == user.tenant_id` in addition to RLS (defense-in-depth, matching `_get_owned` and `list_reports` post-#181).
6. **Report iframes are ALWAYS `sandbox=""`** (empty sandbox — scripts never run in report HTML). Blob URLs revoked on cleanup, `cancelled`-flag pattern per the house standard.
7. **Frontend**: `apiClient` only, `"use client"` on hook files, react-query invalidation per house pattern, lucide icons, house tokens (`rounded-xl border bg-card p-5 shadow-soft`, `text-[15px]`/`text-[13px]`).
8. **No LLM writes numbers** — the wall renders the frozen artifact verbatim; no new number path.
9. **Backend tests from the worktree**: `cd /Users/aidenyi/projects/ecom-netsuite-suites/.claude/worktrees/feat-dashboard-wallpaper/backend && /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest <target> -q`. Sandbox blocks the local DB socket — re-run DB-backed commands with the sandbox disabled. NEVER run alembic against Supabase/remote.
10. **Frontend tests**: `cd .../feat-dashboard-wallpaper/frontend && npx vitest run <target>`; `npx tsc --noEmit` must stay clean. Run `npm install` once if `node_modules` is absent.
11. Ruff `check` AND `format --check` clean on touched backend files.
12. Commit when green; one commit per logical change; never amend.

## Task 1: Backend — per-user active dashboard (migration + model + service)

**Files owned**: `backend/alembic/versions/<new>.py`, `backend/app/models/user_dashboard_preference.py` (new), `backend/app/models/__init__.py` (if it aggregates models), backend test file(s).

- Migration `092_user_dashboard_preference` — parent = the CURRENT single head (verify by walking the revision graph; **never** create a merge migration; the graph is a single chain 001→091 today). New table `user_dashboard_preferences`:
  - `id` uuid PK, `tenant_id` uuid FK `tenants.id` ON DELETE CASCADE (indexed), `user_id` uuid FK `users.id` ON DELETE CASCADE (indexed), `report_id` uuid FK `reports.id` **ON DELETE CASCADE** (so deleting/`unpublishing`-then-deleting a report simply drops the selection and the user falls back), `updated_at` timestamptz.
  - **UNIQUE (tenant_id, user_id)** — one active choice per user per tenant.
  - RLS: enable + FORCE row level security with the same tenant policy shape the other tenant tables use (copy the pattern from migration 084 / whichever is canonical — research it, do not invent).
  - Downgrade drops the table.
- Model per house style (`Mapped[]` + `mapped_column()`).
- Research how the DB test harness provisions schema so the new table exists for DB-backed tests.
- Tests: table exists with the unique constraint; inserting two rows for the same (tenant, user) violates it; deleting the referenced report removes the preference row (cascade).

## Task 2: Backend — dashboard API

**Files owned**: `backend/app/api/v1/dashboard.py` (new), `backend/app/api/v1/router.py` (register), `backend/app/schemas/dashboard.py` (new), backend test file(s).

Single read endpoint so the landing page does not over-fetch the whole report list (a gate finding on #181):

- `GET /api/v1/dashboard` → `DashboardResponse`:
  - `published: list[ReportResponse]` — reports where `dashboard_pinned_at IS NOT NULL`, tenant-scoped, ordered `dashboard_pinned_at DESC`.
  - `active: ReportResponse | None` — the user's selection if it is still published, else the most recently published, else `None`.
  - `active_is_fallback: bool` — `true` when the user HAD a stored selection that is no longer available (deleted or unpublished) and we substituted. Drives the one-time notice. `false` when the user simply never chose.
- `PUT /api/v1/dashboard/active` body `{"report_id": "<uuid>"}` → 200 `DashboardResponse`:
  - 404 (identical shape to the reports routes, `{"detail": "Report not found"}`) for unknown id, malformed uuid, or a report not visible to this tenant.
  - **409** with detail exactly `"That report isn't published to the dashboard"` when the report exists but `dashboard_pinned_at IS NULL`.
  - Upsert the preference row (unique on tenant+user). Audit `dashboard.select`.
  - **No permission gate beyond `get_current_user`** — choosing your own wallpaper is a personal preference; anyone who can view reports may choose one. State this in a comment.
- `DELETE /api/v1/dashboard/active` → 200 `DashboardResponse` — clears the selection (back to fallback). Idempotent. Audit `dashboard.clear`.
- Stale-selection self-heal: when the stored selection is unavailable, the GET returns the fallback but does **not** delete the row (a report can be re-published; deletion is handled by the FK cascade).
- Tests: empty tenant → `active: null`, `published: []`; never-chosen user → most recent published, `active_is_fallback: false`; chosen → that one; chosen-then-unpublished → fallback with `active_is_fallback: true`; PUT of an unpublished report → 409 exact detail; PUT unknown/malformed → 404; cross-tenant report id → 404; DELETE clears; audit rows written.

## Task 3: Frontend — the wall

**Files owned**: `frontend/src/hooks/use-dashboard.ts` (new), `frontend/src/app/(dashboard)/dashboard/page.tsx`, `frontend/src/app/(dashboard)/dashboard/dashboard-wall.tsx` (new), test files. **Delete** `pinned-report-card.tsx` and its test once nothing imports them.

- `useDashboard()` → GET `/api/v1/dashboard`, key `["dashboard"]`.
- `DashboardWall` renders the active report **full size** in the same empty-sandbox iframe pattern:
  - `scale = Math.min(1, containerWidth / 1120)` — fit down on narrow screens, **never scale up** past 1:1.
  - Container height `72vh, min 520px`; the report scrolls *inside* the iframe exactly as on the report page. This retires the crop-height problem entirely (interim fix PR #185 becomes moot).
  - Header row above the wall: report title (semibold) · freshness chip (reuse the existing green/amber/snapshot logic — move it into a shared component rather than duplicating) · right side `Switch ▾` (Task 4) and a ghost `Open ↗` link to `/reports/{id}`.
  - Greeting line `Welcome back, {first name}` stays, demoted beneath the header per the mock.
  - Loading = house Skeleton at the wall's dimensions; fetch error = `Preview unavailable` with the title row still linked.
- Quick Access grid stays, as a slim row **below** the wall (the mock's 4 compact tiles).
- Tests: renders the active report's iframe with `sandbox=""`; revokes the blob on unmount; scale never exceeds 1; freshness variants; error state.

## Task 4: Frontend — the switcher

**Files owned**: `frontend/src/hooks/use-dashboard.ts` (mutations), `frontend/src/app/(dashboard)/dashboard/dashboard-switcher.tsx` (new), `dashboard-wall.tsx` (wire it in), test files.

- `useSetActiveDashboard()` → PUT; `useClearActiveDashboard()` → DELETE. Both invalidate `["dashboard"]`.
- `Switch ▾` opens a DropdownMenu (reuse the existing Radix component — note: its trigger opens on **pointerdown**, so jsdom tests need `pointerDown` then `click`, per PR #181's `openOverflowMenu` helper):
  - Group label `Published dashboards`; one item per published report, `✓` on the active one, right-aligned meta = its `auto_refresh` value (`daily`/`hourly`/`off` → render `off` as `snapshot`).
  - Divider, then `Manage published set…` linking to `/reports`.
  - Selecting an item fires the PUT and the wall swaps — instant, no recompute. Disable items while the mutation is pending.
  - When `published.length <= 1`, still render the menu (it carries `Manage published set…`).
- **Fallback notice**: when `active_is_fallback` is true, show a dismissible one-line banner above the wall: `The dashboard you had chosen is no longer available — showing {title} instead.` Dismissal is per session (component state is fine; do not persist).
- Tests: menu lists published reports with the tick on active; choosing one calls PUT with that id and invalidates; pending disables; fallback banner shows only when the flag is true and dismisses.

## Task 5: Frontend — publish rename, manage view, empty state

**Files owned**: `frontend/src/app/(dashboard)/reports/[id]/page.tsx` (button copy), `frontend/src/app/(dashboard)/reports/page.tsx` (published section), `frontend/src/hooks/use-reports.ts` (naming only if needed), test files.

- Report toolbar: `Pin to dashboard` → **`Publish to dashboard`**; `Unpin from dashboard` → **`Unpublish from dashboard`**. Icons unchanged. (Endpoints and audit action names stay `pin`/`unpin` — the API is shipped; this is a copy change only. Note that in a comment so nobody "fixes" the mismatch.)
- Reports list: a compact **Published dashboards** section above the report list showing published reports with `PUBLISHED` / `ON YOUR WALL` pills and an `Unpublish` action (mock §3). Only render the section when at least one report is published.
- Dashboard empty state (nothing published): dashed card, `No dashboard on the wall yet`, body `Compose a report, then choose Publish to dashboard to put it here. You can publish several and switch between them anytime.`, plus a `Browse reports →` button linking to `/reports`. Quick Access still renders below.
- Tests: toolbar copy both states; published section renders/hides and unpublish fires; empty state copy + link.

## Final gates (after Task 5)

1. Full backend pytest + FE `vitest run` + `tsc --noEmit` + ruff (both).
2. Final whole-branch review (most capable model), with the approved mock as the spec reference and explicit attention to: the RLS-refresh ordering rule (constraint 4), tenant scoping on the new table, and blob/iframe lifecycle across switch operations (switching swaps the displayed artifact — verify no blob leak per switch).
3. Controller eyeball gate: render every surface (wall, switcher open, fallback banner, empty state, manage section) and compare against the mock BEFORE the PR.
4. T2 gate: `Workflow({name: "code-review-multiangle", args: {target: "<PR#>"}})` — fails closed; resolve CONFIRMED + PLAUSIBLE-major findings pre-merge; re-gate until knowns-only.
5. PR; do not merge without operator go-ahead. Close PR #185 as superseded if this lands first.
