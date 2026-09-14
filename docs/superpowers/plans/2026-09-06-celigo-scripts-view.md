# Celigo Scripts view — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the account-wide Celigo Scripts view (clone families, versions, compare, where-used) and wire the drawer, the integration Scripts tab, and the canvas chip into it.

**Architecture:** One new backend service module (`script_families.py`) over the existing clone-family rules, exposed by two read-only gated endpoints; one new frontend surface (`components/celigo/scripts/`) driven by URL params, reusing the drawer's highlighter/shield and the workspace's Monaco diff viewer. Script content stays human-only (N2) and the guard becomes a test.

**Tech Stack:** FastAPI + SQLAlchemy async + Pydantic (backend, pytest); Next 14 / React 18 / TypeScript / TanStack Query / react-resizable-panels v4 / `@monaco-editor/react` (frontend, vitest + testing-library).

**Spec:** `docs/superpowers/specs/2026-09-06-celigo-scripts-view-design.md` (binding). Mock: `docs/superpowers/mockups/2026-09-06-celigo-scripts-view.html`.

## Global Constraints

- TDD: every behaviour gets a failing test first; the report must show the red run.
- Production scripts only (`celigo_script_is_production()`); family = `dedup_key`; version letters by first appearance of a content hash; "original" = `celigo_id == dedup_key` (spec §1 items 3–4).
- The families LIST response never contains `content` or `content_hash`; the DETAIL contains `content` per member. No chat tool, `read_queries.py`, or `services/chat/**` module may import `script_families` (spec §5).
- Both endpoints gated by `require_permission("connections.view")` + `require_feature("celigo")`; no connection → empty list, never 500; unknown family → 404.
- Frontend: `"use client"` on hook/component files; `apiClient` only (never raw fetch); `react-resizable-panels` v4 imports (`Group as PanelGroup`, `Separator as PanelResizeHandle`, `orientation`, percent STRINGS); query state through `queryState()`; icons from `lucide-react`; `cn` for classes. Never print script bodies in reports; fixtures use synthetic script text (`"function preMap(o){return o.data}"`), synthetic names, never real customer identifiers beyond the family names already in the mock.
- Never amend commits; one commit per logical change with trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; stage only your own files.
- Commands: backend `cd backend && .venv/bin/python -m pytest tests/test_celigo_script_families.py tests/api/test_celigo_script_families_api.py tests/api/test_celigo_read_queries_parity.py tests/test_celigo_repository.py tests/test_celigo_topology.py -q` and `.venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests`; frontend `cd frontend && npx vitest run src/components/celigo src/components/settings src/hooks && npx tsc --noEmit -p tsconfig.json && npx eslint <changed files>`.

---

### Task 1: Family facts service (`script_families.py`)

**Files:**
- Create: `backend/app/services/celigo/script_families.py`
- Test: `backend/tests/test_celigo_script_families.py`
- Read first: `backend/app/services/celigo/repository.py` (`list_logical_scripts`, `LogicalScript`, `celigo_script_is_production`), `backend/app/services/celigo/topology.py` (`script_family_facts`, `ScriptFamilyFact`), `backend/app/services/celigo/read_queries.py` (`sync_status`, `_get_celigo_connection`), `backend/app/models/celigo.py` (`CeligoScript`, `CeligoScriptAttachment`, `CeligoFlow`, `CeligoFlowStep`, `CeligoIntegration`, `CeligoFlowError`), `backend/tests/test_celigo_repository.py` and `backend/tests/test_celigo_topology.py` for the seeding pattern the tests should reuse.

**Interfaces:**
- Produces: the dataclasses and the two functions in spec §2.2–2.3, exactly those names and fields. `ScriptFamilySummary.kind` ∈ `hook|transform|filter|router|mixed|unattached`.
- Consumes: nothing new.

**Steps:**
- [ ] Write failing tests (one per rule in spec §2.2): production-only; family key; name rule (original present vs absent); version letters (order by first `celigo_last_modified`, ties by hash; member with NULL hash → `None` letter, no version created); `kind` (single/mixed/unattached); `function_name` mode with alphabetical tie; sites/flows/integrations counts; `open_error_count` per site (open = `resolved_at IS NULL AND purged_at IS NULL`) and `sites_with_open_errors`; `sites_unchecked` from `errors_checked_at`; `other_families_with_name`; totals reconcile with the list; sort order; router-level site (`flow_step_id` None) → `open_error_count None`; list has no content/hash; detail members carry content and are ordered; `get_script_family` unknown → `None`; sandbox family → `None`.
- [ ] Run: expect ImportError / failures for the right reasons.
- [ ] Implement with a bounded number of queries (spec §2.3). Share the version-letter rule with `topology.script_family_facts` (extract a helper if needed; existing tests untouched and green).
- [ ] Run the task's test file + `test_celigo_repository.py` + `test_celigo_topology.py`; ruff check + format.
- [ ] Commit: `feat(celigo): script family facts service (clone families, versions, sites, error rollup)`.

---

### Task 2: The two endpoints + N2 import guard

**Files:**
- Modify: `backend/app/api/v1/celigo_flows.py` (new Out models + two routes, declared before `GET /scripts/{script_id}`)
- Test: `backend/tests/api/test_celigo_script_families_api.py`; extend `backend/tests/api/test_celigo_read_queries_parity.py::TestNoScriptContentSelected`
- Read first: the existing `get_script_detail` route and `CeligoScriptOut`, the integrations route's connection resolution, `backend/tests/api/test_celigo_flows_api.py` (client/auth/flag fixtures).

**Interfaces:**
- Consumes: Task 1's `list_script_families` / `get_script_family`.
- Produces: `GET /api/v1/celigo/scripts/families` → `CeligoScriptFamiliesOut`; `GET /api/v1/celigo/scripts/families/{dedup_key}` → `CeligoScriptFamilyOut` (spec §2.4); response JSON keys equal the dataclass field names; datetimes ISO 8601; uuids as strings.

**Steps:**
- [ ] Failing API tests: 401/403 without permission; 403 without the `celigo` flag; no connection → `{"totals": {all zeros}, "families": [], "synced_at": null}`; list carries no `content`/`content_hash` keys anywhere (walk the JSON); detail has `content` per member and 404 for an unknown key; `/scripts/families` is not swallowed by `/scripts/{script_id}`.
- [ ] Failing guard test: an import scan (AST `Import`/`ImportFrom`) over `app/services/celigo/read_queries.py`, `app/mcp/tools/celigo_flow_map.py`, and every module under `app/services/chat/` asserts none imports `script_families`; plus `test_prompt_tool_sync` / `test_chat_tools_celigo_inventory` still pass unchanged (no new tools).
- [ ] Implement; run the API tests + parity tests + `tests/test_prompt_tool_sync.py` + `tests/test_chat_tools_celigo_inventory.py`; ruff.
- [ ] Commit: `feat(celigo): script families endpoints (list + detail) with the N2 import guard as a test`.

---

### Task 3: Route params + hooks + surface toggle

**Files:**
- Modify: `frontend/src/components/celigo/celigo-route.ts` (+ its test `__tests__/celigo-route.test.tsx`), `frontend/src/hooks/use-celigo-flows.ts`, `frontend/src/components/celigo/celigo-surface.tsx`
- Create: `frontend/src/components/celigo/scripts/celigo-scripts-page.tsx` as a minimal shell (renders the crumb + "Scripts" heading + the three query states) so the surface has a destination; Task 4 fills it.
- Read first: `celigo-route.ts` (`readCeligoRoute`, `CELIGO_KEYS`, `go`), `use-celigo-flows.ts` (`useCeligoScript`, `CeligoScript`), `celigo-surface.tsx`, `lib/query-state.ts`, the `Files | Celigo flows` toggle in `app/(dashboard)/workspace/surface-toggle.tsx` (style reference only).

**Interfaces:**
- Produces: route fields `familyKey`, `copyId`, `compare` (`{ left: string; right: string } | null`), `scriptsFilter` (`"all"|"attached"|"unattached"|"diverged"|"errors"`), `scriptsKind` (`"hook"|"transform"|"filter"|"router"|"unattached"|null`), `scriptsIntegrationId` (from the `in` param, string | null), `q` (string); `go.scripts({ family?, copy?, in?, filter?, kind?, q?, compare? })` (never sets `integration`); `isScriptsView(route)` = `tab === "scripts" && !integrationId && !flowId`; hooks `useCeligoScriptFamilies()` and `useCeligoScriptFamily(dedupKey: string | null)`; TS types `CeligoScriptFamilySummary`, `CeligoScriptFamiliesList`, `CeligoScriptFamilyTotals`, `CeligoScriptFamilyMember`, `CeligoScriptFamilyVersion`, `CeligoScriptFamilySite`, `CeligoScriptFamilyDetail` mirroring spec §2.2 field-for-field (snake_case as the API sends).
- Consumes: Task 2's endpoints.

**Steps:**
- [ ] Failing route tests: each new param round-trips through `readCeligoRoute` → `go.scripts` → URL; invalid `filter`/`kind`/`compare` fall back (`all`, `null`, `null`); `tab=scripts` without `integration` is the Scripts view (`isScriptsView` true); `integration=X&tab=scripts` is NOT the Scripts view (it stays the integration page's Scripts tab); the integration filter travels as `in=` and `go.scripts` never writes `integration`.
- [ ] Failing surface tests: the `Flow map | Scripts` group renders with `aria-pressed` reflecting the route; Scripts active → the scripts page shell mounts; Flow map click → integrations list.
- [ ] Implement; vitest for `src/components/celigo`, tsc, eslint.
- [ ] Commit: `feat(celigo): scripts route params, family hooks, Flow map | Scripts toggle`.

---

### Task 4: Scripts page + list pane

**Files:**
- Modify: `frontend/src/components/celigo/scripts/celigo-scripts-page.tsx`
- Create: `frontend/src/components/celigo/scripts/celigo-scripts-list.tsx`, `frontend/src/components/celigo/scripts/family-row.tsx` (the row, reused by Task 6's integration tab), `frontend/src/components/celigo/scripts/__tests__/celigo-scripts-page.test.tsx`, `.../celigo-scripts-list.test.tsx`
- Read first: `celigo-integrations-page.tsx` (tiles + list patterns), `celigo-integration-page.tsx` (`ScriptsCell`, table style), `shared.tsx` (`ErrorNotice`, pills), the mock's State one list pane and tiles.

**Interfaces:**
- Consumes: Task 3's hooks/route.
- Produces: `CeligoScriptsPage` (tiles, split pane, empty/query states, selection = `route.familyKey`), `CeligoScriptsList({ families, totals, selectedKey, onSelect, filter, kind, q, integrationId, onFilterChange, onKindChange, onQueryChange, onIntegrationChange })`, `FamilyRow({ family, selected, onSelect, compact? })`, and pure helpers `filterFamilies(families, { filter, kind, q, integrationId })` and `groupFamiliesByKind(families)` (exported, unit-tested).

**Steps:**
- [ ] Failing tests: tiles show totals and clicking one sets the filter (URL `filter=`); grouping order Hooks → Transforms → Filters → Routers → Mixed → Unattached with header counts; search matches name, function, flow names (case-insensitive) and the footer says "Showing X of Y families"; chips + integration select filter; "N families with this name" appears only when `other_families_with_name > 0`; copies pill amber only when diverged; ArrowUp/Down/Enter selection; pending state renders a skeleton (never "0"); error state renders `ErrorNotice`; not-synced (`synced_at === null`) renders the exact copy from spec §3.3; the page contains no button labelled deploy/push/save/edit/run and no `<form>`.
- [ ] Implement (panel group with percent strings; list default `34%`, min `24%`).
- [ ] vitest, tsc, eslint. Commit: `feat(celigo): Scripts view — tiles, list pane, search and filters`.

---

### Task 5: Detail pane — versions, source, compare, where-used

**Files:**
- Create: `frontend/src/components/celigo/scripts/celigo-scripts-detail.tsx`, `.../__tests__/celigo-scripts-detail.test.tsx`
- Modify: `frontend/src/components/workspace/diff-viewer.tsx` (add optional `sideBySide?: boolean` default `true` and `language?: string` override; existing callers unchanged), `celigo-scripts-page.tsx` (mount the detail for `route.familyKey`)
- Read first: `components/settings/celigo-script-viewer.tsx` (highlighter style + `UntrustedContentBanner`), `components/workspace/diff-viewer.tsx`, the mock's States one to three.

**Interfaces:**
- Consumes: `useCeligoScriptFamily`, route `copyId`/`compare`, `go.flow`, `go.scripts`.
- Produces: `CeligoScriptsDetail({ dedupKey })` and pure helpers `defaultComparePair(versions)` (oldest → the version holding the original, else oldest → newest) and `versionForCopy(members, copyId)`.

**Steps:**
- [ ] Failing tests: header facts; versions strip cards (letter, copies, sites, first seen, size, `✓ original` when `holds_original`, `spare copy` when 0 sites); arriving with `copy=` selects that member's version; Compare disabled when one version; compare mode shows the default pair, pickers change the pair and the URL (`compare=A..D`), Side by side / Inline toggles the `sideBySide` prop (mock `DiffViewer`); source bar text for original vs clone; the shield copy verbatim; where-used cells: paused pill, step reference name with role · adaptor sub-line, mono json_path, copy column `C · original` / `B · clone 24 Mar 2026`, errors `1 open` (crit) / `0` / `not checked` / `—` for router sites; `↗` navigates with flow+step+site; 8 rows then "8 of 16 shown · Show all 16"; Open in flow map disabled for an unattached family; Copy source writes the shown version to the clipboard (mock).
- [ ] Implement; vitest (also `src/components/workspace`), tsc, eslint.
- [ ] Commit: `feat(celigo): Scripts view — family detail with versions, compare, where-used`.

---

### Task 6: Entry points — drawer link, integration Scripts tab, canvas chip

**Files:**
- Modify: `frontend/src/components/settings/celigo-script-viewer.tsx` (+ `settings/__tests__/celigo-script-viewer.test.tsx`), `frontend/src/components/celigo/celigo-script-drawer.tsx` (+ test), `frontend/src/components/celigo/celigo-integration-page.tsx` (`ScriptsTab`, + test), `frontend/src/components/celigo/step-bubble.tsx` and/or `celigo-flow-canvas.tsx` (chip modifier-click, + tests)
- Read first: the "Scripts view ↗" span and its comment in the viewer; `ScriptsTab`; how the step chip's click opens the drawer (`step-bubble.tsx`, `celigo-flow-page.tsx`).

**Interfaces:**
- Consumes: `go.scripts`, `FamilyRow`, `useCeligoScriptFamilies`.
- Produces: `CeligoScriptViewerBody` prop `onOpenScriptsView?: () => void` (inert text when absent — the settings dialog); the drawer passes it and closes on use; `ScriptsTab` lists the integration's families (client-side filter by `integration_ids`) with `FamilyRow compact` and the link "Open in Scripts view, filtered to this integration ↗" = `go.scripts({ in: integrationId })`; chip ⌥/Alt or ⌘/Meta click → `go.scripts({ family, copy })`, plain click unchanged.

**Steps:**
- [ ] Failing tests: viewer renders a button "Scripts view ↗" only with the prop and calls it; without the prop the inert text remains (settings dialog test); drawer test: clicking it navigates to `tab=scripts&family=<dedup_key>&copy=<id>` and closes; integration tab lists only families with a site in that integration, shows "N families · M sites here", and the link navigates with `tab=scripts&in=<integrationId>` and no `integration` param; chip modifier-click navigates, plain click still opens the drawer.
- [ ] Implement; vitest for celigo + settings, tsc, eslint.
- [ ] Commit: `feat(celigo): Scripts view entry points — drawer link, integration Scripts tab, chip modifier-click`.

---

## Verification (after Task 6)

- `scripts/verify.sh --full` against the isolated verify DB; frontend suites + tsc + eslint; backend ruff.
- T2 gate on the backend half (`code-review-multiangle`, target = the PR) before merge; the in-workflow advisory review runs on the whole diff.
- Live check on staging (Framework): the five tiles show 129 · 67 · 31 · 14 · N; `ns_sales_order_premap` selected shows 7 copies, A/B/C with 1/3/3 copies, original on C, 16 sites in the table; `FW Sales Order Hook` compare A → D renders the Monaco diff; `Framework 945 v2` shows the unattached note; the drawer link from the Multi-Subsidiary flow lands on the family with the right copy selected; the integration Scripts tab lists 14 families for Solidus + NetSuite.
