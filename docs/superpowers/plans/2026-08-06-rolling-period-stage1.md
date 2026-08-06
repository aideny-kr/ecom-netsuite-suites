# Rolling period — Stage 1: the wall tracks the last closed period

**Approved design**: artifact `rolling-period-mock-v1` — https://claude.ai/code/artifact/58b18457-7343-4be3-b800-ed11bffde3d5 (operator-approved 2026-08-06: "yes that looks better"). The rendered UI is the acceptance gate, not green tests.

**Ticket**: ClickUp 86bb8ndzk (claimed). **Tier**: T2 (migration + a path that writes customer-facing artifacts). **Branch**: `feat/rolling-period` off `44e68db`.

## Why

A report's period is typed once and frozen into the recipe (`playbooks.py::_source`), and refresh replays it verbatim. Fine for a one-off artifact; wrong for a wallpaper — the dashboard implies "current" and silently becomes a historical exhibit.

**Grounded in live Framework data (2026-08-05):** `Jun 2026` is closed; `Jul 2026` has ended on the calendar but is **still open** in NetSuite; periods exist through `Dec 2026` (all open); and an `Adjust 2025` row exists whose name does not match the `"Mon YYYY"` validator. So neither "newest period" nor "last finished month" is correct — only `closed = 'T'` is.

## Shape: roll the WALL, not the report

Rolling a single report in place was rejected, for reasons found in the code:
- `report.title` is written once at compose (`playbooks.py:173`) and becomes the page `<h1>`; refresh reads it but never recomputes it (`refresh_service.py:334`). The statement narrative reads the period **live** (`statement_builder.py:823`). A rolling report would permanently render `— Jun 2026` as its heading over July's text and numbers.
- Version history would blend periods under one identity; the version picker shows only `v4 · current · Jul 22` with no period signal.
- Refresh publishes a version **unconditionally** every tick, so a daily-refreshed monthly report yields ~29 no-op versions per cycle against a 30-version retention cap — aging out the one transition that mattered.

So: reports stay immutable artifacts of one period. A **series** tracks a playbook, and the wall follows the series' newest report.

**Stage 1 (this branch)** = resolver + series + wall follows it + UI. **Stage 2 (next)** = the scheduled compose that creates each new period's report automatically. Stage 1 is useful alone: composing next month's report by hand moves every wall that follows the series.

## Global Constraints (binding for every task)

1. **TDD strictly** — failing test first, watch it fail, then implement. A test never shown red is not a test.
2. **`./scripts/verify.sh` is the evidence node.** Exit 0 or the task is not done. `--quick` is inner-loop only and is NOT evidence.
3. **Never `db.refresh()` or query an RLS table after `db.commit()`** — the COMMIT clears the `SET LOCAL` tenant GUC; a refresh 500s and a plain SELECT **silently returns zero rows**. Build responses from pre-commit data. The harness cannot see either failure (savepoints + BYPASSRLS).
4. **Every mutation audit-logged** before `await db.commit()`, `category="dashboard"` for dashboard-family actions (match the existing `dashboard.select` / `dashboard.clear` filing).
5. **Explicit `tenant_id` predicate on every query**, in addition to RLS.
6. **Do NOT touch** `report_html.py`, `statement_builder.py`, `refresh_service.py`, or `netsuite_financial_report.py`'s SQL templates. Stage 1 adds alongside; it does not modify the render or replay paths.
7. **Structural questions go to `python3 scripts/codegraph.py`** (`def` · `callers` · `uses` · `importers` · `routes` · `impact`), not grep.
8. **Frontend**: `apiClient` only, `"use client"` on hook files, house tokens, react-query invalidation per house pattern.
9. Ruff `check` AND `format --check` clean over CI's scope (`app/`, `tests/`).
10. Commit when green; one commit per logical change; never amend.

## Task 1: Period resolver

**Files owned**: `backend/app/services/report/period_resolver.py` (new), its test file.

- `async def resolve_last_closed_period(db, tenant_id) -> ClosedPeriod | None` returning the period **name** (NetSuite's own `periodname`, which `build_period_filter` matches on) plus its `enddate`.
- Query `accountingperiod` via the **local** SuiteQL path (`netsuite_suiteql.execute`) — statement refreshes already run through it, and `accountingperiod` is in `NETSUITE_SUITEQL_ALLOWED_TABLES` (`config.py:87-97`). Precedent for calling it directly from a service: `metric_compute.py:254`.
- Predicate: `closed = 'T' AND isadjust = 'F' AND isquarter = 'F' AND isyear = 'F'`, newest by `enddate`. **All four matter** — future periods exist and are open; `Adjust 2025` is an adjustment row whose name isn't a month.
- Also return whether a **later, still-open** period exists (name + enddate) — the ribbon says "Jul 2026 is still open", and that string comes from data, not inference.
- The returned name must satisfy `netsuite_financial_report._validate_period_name`'s regex. If NetSuite returns a name that doesn't (13-period or 4-4-5 calendars, renamed periods), **return None rather than a value that will fail downstream**, and say so in the reason. Do not attempt to reformat it.
- Return a reason enum for the None cases: `no_closed_period` | `unsupported_period_name` | `unreachable`. Never raise for "couldn't determine" — the caller must be able to degrade.
- Tests: newest-closed selection; open/future periods excluded; adjustment row excluded; quarter/year rollups excluded; non-conforming name → None + reason; empty result → `no_closed_period`; a SuiteQL error → `unreachable` (not an exception escaping).

## Task 2: Series model + migration 093

**Files owned**: `backend/alembic/versions/093_*.py`, `backend/app/models/report_series.py` (new), `backend/app/models/report.py` (add the two columns), test file.

- Table `report_series`: `id` uuid PK; `tenant_id` uuid FK → tenants CASCADE, indexed; `playbook_key` text; `created_by` uuid FK → users SET NULL; `created_at`. **UNIQUE (tenant_id, playbook_key)** — one tracking series per playbook per tenant. RLS ENABLE + FORCE with the canonical tenant policy (copy 084's shape verbatim; do not invent one).
- `reports` gains: `series_id` uuid FK → `report_series.id` **ON DELETE SET NULL** (deleting a series must not delete its reports — they remain valid artifacts), indexed; and `period` text nullable (the canonical `"Mon YYYY"` this report covers, denormalized from the recipe so the newest-in-series query doesn't parse JSON).
- **UNIQUE (series_id, period)** where both are non-null — this is what makes Stage 2's compose idempotent. Use a partial unique index.
- Backfill: leave `period`/`series_id` NULL on existing rows. Every existing report is a snapshot; nothing is retro-assigned.
- Verify single-head lineage (093 ← 092) before and after; never a merge migration.
- Tests: constraints exist and bite (duplicate series per playbook → IntegrityError; duplicate period in a series → IntegrityError; two NULLs allowed); deleting a series leaves its reports with `series_id` NULL; RLS enabled+forced asserted against `pg_class`/`pg_policy`.

## Task 3: Compose with a tracking intent

**Files owned**: `backend/app/services/report/playbooks.py`, `backend/app/api/v1/reports.py` (the playbook compose endpoint), `backend/app/schemas/report.py`, test files.

- `POST /api/v1/reports/playbooks/{key}` accepts `mode: "period" | "tracking"` (default `"period"` — existing behaviour is untouched and remains the default for every existing caller).
- `mode="tracking"`: resolve the last closed period (Task 1). If it returns None, **400 with the reason** — do not compose a report for a guessed period. Then get-or-create the tenant's series for that playbook, compose the report as today for the resolved period, and set `series_id` + `period` on it.
- `mode="period"`: exactly today's behaviour, plus setting `period` on the report (series_id stays NULL).
- Idempotent: composing tracking twice for the same closed period returns the existing report rather than creating a second (the partial unique index is the backstop; the endpoint should not rely on catching IntegrityError as flow control).
- Audit `report.compose` as today; add the series id to the payload when one applies.
- Tests: tracking composes and links to a new series; a second tracking compose for the same period returns the same report; resolver-None → 400 naming the reason; `mode="period"` byte-identical to today's behaviour (assert the existing tests still pass unchanged).

## Task 4: The wall follows a series

**Files owned**: `backend/app/api/v1/dashboard.py`, `backend/app/schemas/dashboard.py`, `backend/alembic/versions/094_*.py` (preference gains `series_id`), `backend/app/models/user_dashboard_preference.py`, test files.

- `user_dashboard_preferences` gains `series_id` uuid FK → report_series SET NULL, nullable. A preference selects **either** a report (snapshot) **or** a series (tracking) — enforce a CHECK that at most one is non-null.
- `PUT /api/v1/dashboard/active` accepts `{"report_id": …}` (today) **or** `{"series_id": …}`. Exactly one required; both or neither → 400. Same 404 parity for an unknown/cross-tenant series.
- `GET /api/v1/dashboard` resolves a series selection to its **newest report by `period`** (order by the report's `enddate`-equivalent — use `period` ordering via the series' reports' `created_at` only as a tiebreaker, never as the primary sort, because compose order ≠ period order).
- `published` gains the tracking series as selectable entries alongside published reports — the switcher needs both kinds (mock §5). Keep the existing report entries exactly as they are.
- Response gains, for a tracking selection: the resolved period, and whether a later period is open (from Task 1, cached per request — **one resolver call per GET at most**, and its failure must degrade to "can't tell", never fail the request).
- Existing behaviour for report-selections is unchanged, including `active_is_fallback` and the dismiss path.
- Tests: series selection resolves to the newest report; a series with no reports yet → the empty/`can't tell` shape, not a 500; resolver failure degrades; both-ids → 400; cross-tenant series → 404; report-selection tests all still pass untouched.

## Task 5: Frontend — chooser, ribbon, switcher

**Files owned**: `frontend/src/app/(dashboard)/reports/playbook-launcher.tsx`, `frontend/src/app/(dashboard)/dashboard/dashboard-wall.tsx`, `dashboard-switcher.tsx`, `frontend/src/hooks/use-dashboard.ts`, `use-reports.ts`, test files.

- **Launcher (mock §2)**: two options — *Track the last closed period* (default-selected, shows the resolved period as a hint) and *One specific period* (today's input). Selecting tracking hides the period input.
- **Ribbon (mock §3)** above the wall, three states, copy verbatim from the mock:
  - green — `Last closed period · {period}` + `— {next} is still open in NetSuite. This wall moves to {next-short} the day it closes.`
  - amber — `{period} closed {n} days ago — building {month}'s statement now.` (Stage 1 can only reach this once Stage 2 exists; render it if the API reports it, otherwise never.)
  - grey — `Couldn't reach NetSuite to check the close — showing {period} from {date}.`
  - The ribbon renders **only for a tracking selection**. A pinned snapshot shows no ribbon (it isn't tracking anything).
- **Switcher (mock §5)**: two groups — `Tracking the close` (series, meta = current period) and `Pinned months` (reports, meta = `snapshot`). Selecting either issues the right id to `PUT /active`.
- Radix `DropdownMenuTrigger` opens on **pointerdown** — jsdom tests need `pointerDown` then `click`.
- Tests: chooser toggles the period input; each ribbon state renders from its API shape and the ribbon is absent for snapshots; switcher lists both groups and sends the correct id.

## Final gates

1. `./scripts/verify.sh` (full) — the evidence node, exit 0.
2. Controller eyeball gate: render the launcher chooser, all three ribbon states, and the grouped switcher; compare against `rolling-period-mock-v1` BEFORE the PR.
3. Final whole-branch review (most capable model).
4. `./scripts/ship.sh` → confirms the tier and prints the pinned gate command; run the T2 gate from **inside this worktree** so a `target: null` result still resolves to this branch, and sanity-check that the findings cite files from this diff.
5. Budget 2+ gate rounds. A first-round zero on a large diff is a reason to re-check, not to ship.
6. Update `STATE.md` (NOW → DECIDED) at the end of every task, not "later".
7. PR; do not merge without operator go-ahead.
