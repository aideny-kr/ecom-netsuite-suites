# Celigo Scripts view — design

**Date:** 2026-09-06 · **Status:** approved by the operator (mock) · **Mock (binding):** `docs/superpowers/mockups/2026-09-06-celigo-scripts-view.html` (published at https://claude.ai/code/artifact/6fba6d55-59c1-4a04-850e-b33fbde4bba9) · **Proposal it replaces:** `docs/superpowers/mockups/2026-09-02-celigo-scripts-view.html` (#219)

## 1. What this is

An account-wide **Scripts** view beside the Celigo flow map: every production script in the account collapsed into its **clone family**, grouped by the kind of site that runs it, with the source, the family's copies and versions, a compare-any-two-versions diff, and a where-used table on one screen. It is where the script drawer's inert "Scripts view ↗" label finally goes, where the integration page's Scripts tab stops saying "ships separately", and where a step's script chip can jump.

Decisions taken with the operator on 2026-09-06 (each is binding):

1. **Account-wide, beside the flow map.** Not a per-integration tab: 51 of 129 scripts are attached to no flow (no integration) and 4 families span integrations. The integration page and the drawer link INTO the view pre-filtered.
2. **Search covers names, function names, and flow names**, client-side. Content search is a later slice.
3. **A family is Celigo's clone lineage** (`celigo_scripts.dedup_key` = `COALESCE(source_id, celigo_id)`), never the name. Eight single-copy families are all called `inventory_sync_filter`; the list shows "N families with this name" instead of merging them.
4. **Versions by content, original by lineage.** Version letters (A, B, C…) are distinct `content_hash`es ordered by first appearance (`min(celigo_last_modified)`). The **original** is the family's clone root (the member whose `celigo_id == dedup_key`); it is shown as a mark on whichever version it currently has, because Celigo lets it be edited after clones were made (the largest family's original runs C while three older clones run B).
5. **Compare any two versions**, default oldest content → the original's version (or the newest when no original is in production), rendered by the workspace's existing Monaco diff viewer (`frontend/src/components/workspace/diff-viewer.tsx`), read-only, side-by-side or inline. No new dependency.
6. **The drawer stays** as the in-flow quick look (#223/#224). The Scripts view is where the whole family lives. The link between them is real in both directions.
7. **Human-only, as before (N2).** Script content never reaches the assistant, RAG, or any chat tool. The new endpoints are served like the drawer's; the chat tools never gain a content path (§5).
8. **Per-site open errors** reuse the flow map's error sync (#221) and its honesty stamp: a site whose flow has never had its errors checked reads "not checked", never 0.
9. **Tier:** backend T2 (new read endpoints over customer JavaScript → blocking multi-angle gate pre-merge), frontend T1.

Out of scope now: content search, a change feed across syncs, "what does this script do" in words, editing/deploying/pushing anything to Celigo from this surface (never), sandbox scripts.

## 2. Backend

### 2.1 Module

`backend/app/services/celigo/script_families.py` — a NEW module, separate from `read_queries.py` (which the AST guard keeps content-free) and never imported by `read_queries.py`, `mcp/tools/celigo_flow_map.py`, or anything under `services/chat/`. It reuses `celigo_script_is_production()` and the family rule from `repository.list_logical_scripts` / `topology.script_family_facts` (same grouping key, same version-letter order) rather than reimplementing them ad hoc; if a shared helper is extracted, both existing callers keep byte-identical results (their tests stay green untouched).

### 2.2 Dataclasses (frozen)

```python
@dataclass(frozen=True)
class ScriptFamilySite:
    attachment_id: uuid.UUID
    script_id: uuid.UUID | None          # NULL when the referenced script row was never synced
    script_celigo_id: str
    version_letter: str | None           # None when script_id is None or the member has no content_hash
    integration_id: uuid.UUID | None
    integration_name: str | None
    flow_id: uuid.UUID
    flow_name: str
    flow_disabled: bool | None
    flow_step_id: uuid.UUID | None       # None for router-level sites
    step_reference_name: str | None      # celigo_flow_steps.reference_name (the name the operator gave the step)
    step_role: str | None                # 'generator' | 'processor'
    step_adaptor_type: str | None
    step_record_type: str | None
    step_operation: str | None
    json_path: str
    function_name: str | None
    site_type: str                       # hook | filter | transform | router | unknown
    open_error_count: int | None         # None when flow_step_id is None (router sites have no step errors)
    errors_checked_at: datetime | None   # the flow's honesty stamp; None = never verified

@dataclass(frozen=True)
class ScriptFamilyMember:
    script_id: uuid.UUID
    celigo_id: str
    name: str
    is_original: bool                    # celigo_id == dedup_key
    version_letter: str | None
    content_hash: str | None
    size_bytes: int | None               # len(content) in bytes (utf-8), None when content is None
    celigo_last_modified: datetime | None
    sites_count: int
    flows_count: int
    content: str | None                  # detail only; the human-only body

@dataclass(frozen=True)
class ScriptFamilyVersion:
    letter: str
    content_hash: str
    copies_count: int
    sites_count: int
    first_seen: datetime | None
    size_bytes: int | None
    holds_original: bool

@dataclass(frozen=True)
class ScriptFamilySummary:
    dedup_key: str
    name: str                            # the original's name if present in production, else the earliest-modified member's
    kind: str                            # 'hook' | 'transform' | 'filter' | 'router' | 'mixed' | 'unattached'
    function_name: str | None            # most common function_name across sites (ties → alphabetical); None when unattached
    copies_count: int
    versions_count: int                  # distinct non-null content hashes, min 1 when any member has content
    content_diverged: bool               # versions_count > 1
    original_present: bool
    sites_count: int
    flows_count: int
    integrations_count: int
    integration_ids: list[uuid.UUID]
    flow_names: list[str]                # distinct, sorted — for client-side search
    sites_with_open_errors: int          # sites whose step has ≥1 open error
    sites_unchecked: int                 # sites whose flow.errors_checked_at is None
    first_modified: datetime | None
    last_modified: datetime | None
    max_size_bytes: int | None
    other_families_with_name: int        # how many OTHER families share this family's name

@dataclass(frozen=True)
class ScriptFamilyTotals:
    scripts: int
    families: int
    attached_families: int
    unattached_families: int
    diverged_families: int
    sites: int
    flows_with_sites: int
    flows_total: int
    integrations_with_sites: int
    sites_with_open_errors: int

@dataclass(frozen=True)
class ScriptFamiliesList:
    totals: ScriptFamilyTotals
    families: list[ScriptFamilySummary]  # sorted: sites_count desc, copies_count desc, name asc, dedup_key asc
    synced_at: datetime | None           # last successful flow-map sync (read_queries.sync_status), None = never

@dataclass(frozen=True)
class ScriptFamilyDetail:
    summary: ScriptFamilySummary
    members: list[ScriptFamilyMember]    # ordered: celigo_last_modified asc nulls last, then celigo_id
    versions: list[ScriptFamilyVersion]  # ordered by letter
    sites: list[ScriptFamilySite]        # ordered: integration_name, flow_name, json_path
```

Rules that need a test each: production-only (sandbox excluded); family key; name rule; version letters (order, `None` for members without hash, a member with `None` hash does not create a version); `kind` (single site_type → that; several → `mixed`; none → `unattached`); `function_name` (mode, ties alphabetical); open-error count = `celigo_flow_errors` rows with `flow_step_id = site.flow_step_id AND resolved_at IS NULL AND purged_at IS NULL`; `sites_unchecked`; `other_families_with_name`; totals reconcile with the family list (sum of sites, count of diverged, etc.); sort order; the list carries **no content and no content_hash**; the detail carries content for every member.

### 2.3 Functions

```python
async def list_script_families(db, *, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ScriptFamiliesList
async def get_script_family(db, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, dedup_key: str) -> ScriptFamilyDetail | None
```

Bounded query count (no N+1): one scripts query, one attachments+flows+steps+integrations join, one open-error aggregate grouped by `flow_step_id`, one sync-status read.

### 2.4 API (`backend/app/api/v1/celigo_flows.py`)

Both routes gated exactly like the others in that file: `require_permission("connections.view")` + `require_feature("celigo")`; the tenant's flow-map connection resolved the same way the integrations route resolves it. **No connection → an empty list with `synced_at: null` and zero totals** (honest, not 500).

- `GET /api/v1/celigo/scripts/families` → `CeligoScriptFamiliesOut { totals, families: [CeligoScriptFamilySummaryOut], synced_at }`
- `GET /api/v1/celigo/scripts/families/{dedup_key}` → `CeligoScriptFamilyOut { summary, members: [CeligoScriptFamilyMemberOut], versions: [...], sites: [CeligoScriptFamilySiteOut] }`; 404 `"Script family not found"` when unknown or not production.

Both are declared BEFORE `GET /scripts/{script_id}` in the router. Pydantic Out models mirror the dataclasses field-for-field (datetimes ISO, uuids as strings). `GET /celigo/scripts/{script_id}` is unchanged.

## 3. Frontend

### 3.1 Route (`frontend/src/components/celigo/celigo-route.ts`)

`?surface=celigo&tab=scripts` **with no `integration`** → the Scripts view. New params (all optional, all read/written through `readCeligoRoute` / the `go` helpers, and listed in `CELIGO_KEYS`):

| param | meaning |
|---|---|
| `family` | selected family's `dedup_key` |
| `copy` | a member `script_id`; selects that member's version on arrival (the drawer's hand-off) |
| `compare` | two version letters, `A..D` form; presence = compare mode |
| `filter` | `all` (default) · `attached` · `unattached` · `diverged` · `errors` |
| `kind` | `hook` · `transform` · `filter` · `router` · `unattached` (absent = every kind) |
| `q` | search text |
| `in` | the integration FILTER (families with a site in that integration id). Deliberately NOT `integration`: `integration=X&tab=scripts` already addresses the integration PAGE's Scripts tab and keeps doing so |

`go.scripts({ family?, copy?, in?, filter?, kind?, q?, compare? })` navigates there (it never sets `integration`); `go.flow(...)`/`go.integration(...)` are unchanged. Unknown values fall back to defaults, never throw.

### 3.2 Surface (`celigo-surface.tsx`)

Renders a segmented control **Flow map | Scripts** (`role="group"`, `aria-pressed`) in the row above every Celigo page. Scripts is active exactly when `route.tab === "scripts" && !route.integrationId && !route.flowId` (`isScriptsView(route)`); clicking Flow map goes to the integrations list (or back to the last flow-map location if the surface remembers one — not required). When active, the surface renders `CeligoScriptsPage`. `integration=X&tab=scripts` stays the integration page's Scripts tab.

### 3.3 Components (`frontend/src/components/celigo/scripts/`)

- **`celigo-scripts-page.tsx`** — crumb row ("Celigo › Scripts", synced-ago from sync status), five **stat tiles** (Scripts · Attached · Unattached · Diverged · Sites with open errors; clicking a tile sets `filter`), and the split: list pane | detail pane as a horizontal `react-resizable-panels` group (percent strings; list default `34%`, min `24%`). Query state through `queryState()` like every other page: pending never renders as empty, an error never renders as loading or "0 scripts". Empty states: **not synced yet** (sync status has no successful sync) → "Scripts have not been synced yet. Run the Celigo sync from Settings, then come back."; **no production scripts**; **no match**.
- **`celigo-scripts-list.tsx`** — search box (placeholder "Search scripts, functions, flows"; matches `name`, `function_name`, `flow_names`, case-insensitive substring), chips (`All · Attached · Unattached · Diverged · Errors` + `Integration: any ▾` select), groups by kind in this order: Hooks, Transforms, Filters, Routers, Mixed, Unattached — each group header shows `N sites · M families` (Unattached: `N scripts · M families`), rows: kind badge (`HK`/`TR`/`FL`/`RT`/`MX`/`—`), name, function (muted), "N families with this name" when `other_families_with_name > 0`, copies pill (`×N` plain; `×N · V versions` amber when diverged), `sites · flows` meta; selected row highlighted; keyboard: ArrowUp/Down move selection, Enter opens. Footer: "Showing X of Y families" when filtered.
- **`celigo-scripts-detail.tsx`** — header: badge, name, `N copies`, `V versions` (amber when >1), function, size, modified; buttons **Compare versions ⇄** (disabled when `versions_count < 2`), **Open in flow map ↗** (first site's flow + step + site, disabled when unattached), **Copy source** (clipboard, the shown version). **Versions strip**: one card per version: letter, copies, sites, first seen, size, `✓ original` when `holds_original`, `spare copy` when 0 sites; the selected version is highlighted; arriving with `copy=<id>` selects that member's version. **Source**: bar ("Showing version C · the original copy" / "· a clone"), the body via the same highlighter + inline style as the drawer, followed by the same `UntrustedContentBanner` shield. **Compare mode** (`compare=A..D`): a bar "Comparing A (date) → D original (date)" with two version pickers, Side by side / Inline toggle, then `DiffViewer` (extended with an optional `sideBySide?: boolean` prop, default `true`, and a `language` override so a script does not need a file path). **Where used** table: Integration · Flow (name + `paused` pill when `flow_disabled`) · Step (`step_reference_name` or `role · adaptor` fallback, with `role · adaptor_type` as the sub-line) · Where (`json_path`, mono, no wrap, table scrolls) · Copy (`letter · original` / `letter · clone <date>`) · Errors (`N open` crit pill · `0` ok · `not checked` mute when `errors_checked_at` is null · `—` for router sites) · `↗` = `go.flow(flow_id, { step, script, site })`. Shows 8 rows by default with "N of M shown · Show all M".
- **Entry points:** the drawer's "Scripts view ↗" (`components/settings/celigo-script-viewer.tsx`) becomes a real `<button>`/link only when an `onOpenScriptsView` prop is supplied (the settings dialog supplies none and keeps the inert text) — the drawer calls `go.scripts({ family: script.dedup_key, copy: script.id })` and closes. The integration page's `ScriptsTab` lists that integration's families (client-side filter of the families list by `integration_ids`) with the same row component and a link "Open in Scripts view, filtered to this integration ↗" = `go.scripts({ in: integrationId })`. The canvas step chip: ⌥/Alt-click (or ⌘/Meta) → `go.scripts({ family, copy })`; plain click keeps opening the drawer.
- **Hooks (`frontend/src/hooks/use-celigo-flows.ts`):** `useCeligoScriptFamilies()` → `GET /api/v1/celigo/scripts/families`, `useCeligoScriptFamily(dedupKey | null)` → `GET /api/v1/celigo/scripts/families/{dedupKey}` (`enabled: !!dedupKey`), types mirroring the Out models.

### 3.4 Copy (verbatim)

- Shield: "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant."
- Unattached family note: "No production flow references any of these N copies. None of the M production flows we sync names this script. That does not mean unused: sandbox flows are not synced, and Celigo can reference a script from places this map does not walk."
- Not-synced empty state as in §3.3.

## 4. Tests

- Backend: `backend/tests/test_celigo_script_families.py` (service rules in §2.2, seeded rows through the existing Celigo repository test fixtures/patterns) and `backend/tests/api/test_celigo_script_families_api.py` (gates: 401/403 without permission or flag; no connection → empty; list has no `content`/`content_hash` keys; detail has content per member; 404 unknown; route precedence over `/scripts/{script_id}`).
- N2 guard: `backend/tests/api/test_celigo_read_queries_parity.py::TestNoScriptContentSelected` gains an import-scan test: neither `read_queries.py`, `mcp/tools/celigo_flow_map.py`, nor any module under `services/chat/` imports `script_families`; `test_prompt_tool_sync` / `test_chat_tools_celigo_inventory` known sets unchanged (no new chat tools).
- Frontend: `celigo-route.test.tsx` (new params round-trip, defaults), `scripts/__tests__/celigo-scripts-page.test.tsx` (query states, tiles → filter, empty states), `celigo-scripts-list.test.tsx` (grouping order, search fields, chips, "families with this name", keyboard), `celigo-scripts-detail.test.tsx` (versions strip, original mark, `copy=` arrival, compare default pair + pickers, where-used cells incl. error pills and router `—`, Show all), entry-point tests in `celigo-script-drawer.test.tsx` / `celigo-integration-page.test.tsx` / `step-bubble.test.tsx`. Every test red first.
- Page-level assertion: nothing on the Scripts surface renders a form, a mutation hook, or a button whose label contains "deploy", "push", "save", "edit", "run".

## 5. The N2 boundary, stated once

Script content is customer JavaScript, shown to humans only. It reaches the browser through `GET /celigo/scripts/{id}` (drawer) and, with this design, `GET /celigo/scripts/families/{dedup_key}` (detail). It is never selected by `read_queries.py` (AST test), never returned by any chat tool (`mcp/tools/celigo_flow_map.py` docstring + tests), never embedded, never logged. `script_families.py` is reachable only from the API router; the import-scan test in §4 makes that a build failure, not a comment.
