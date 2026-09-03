# Celigo Flow Pages — spec

**Status:** approved by the operator 2026-09-02 ("approved, include the names sync and start building").
**Design (acceptance artifact):** `docs/superpowers/mockups/2026-09-02-celigo-flow-page.html`
(artifact `https://claude.ai/code/artifact/a7fb145c-bbab-4d6d-9253-41392e65dab1`). The panel's own
mockup, kept for the record: `docs/superpowers/mockups/2026-09-02-celigo-flow-page-dense.html`.
**Ticket:** ClickUp `86bbtxfjw`. **Tier:** T2 (alembic migration, sync job change, workspace shell
change, new read routes over customer JavaScript and error payloads).

## 1. What we are building

Celigo integrator.io's three levels, rebuilt inside the dev workspace (`/workspace`, surface "Celigo
flows"), modernized and dense, every flow as a full page:

1. **My integrations** — tile dashboard (with a list view) over all production integrations.
2. **Integration page** — the integration's flows as a table, plus Scripts / Errors / Changes tabs.
3. **Flow page** — fills the workspace: navigator rail · left-to-right canvas (sources, lookups,
   destinations, routers, branch lanes) · inspector. A hook chip opens the script in a drawer.

The surface is **read-only**: no Run, no Off/On, no retry/resolve, no Sync-now. "Open in Celigo ↗"
is the only way out. It is **production only** (sandbox rows are never shown; the sync already skips
and purges them). Standing decision **N2** holds: script source is shown to humans only, exists only
inside the drawer, and is never sent to the assistant, RAG, search index or any `window.claude`
path. The old flow map (`celigo-flow-map.tsx`'s expand-in-place rows and `FlowDetailDialog`) is
retired by this build.

Included (approved): the **step-names sync** — Celigo's own export/import `name` on each step.
Not included: connection names / per-app logos (separate T2, needs a live probe of credential-
bearing objects), the import's NetSuite field mapping, run history / per-run counts.

## 2. Vocabulary and derived facts

- **Kind** of a step: `source` = generator; `lookup` = processor whose adaptor type is an export
  (`*Export`); `destination` = every other processor. This is Celigo's Source / Lookup / Import
  vocabulary and drives the bubble eyebrow and the left stripe colour.
- **Writes** rollup: per NetSuite record type, count of steps with `record_type` and `operation`
  both set. HTTP/AS2/FTP/RDBMS/REST steps never contribute; an integration with none says
  "no NetSuite writes".
- **Adaptor family** of a step: NetSuite | HTTP | AS2 | FTP | RDBMS | REST, from `adaptor_type`.
- **Schedule shapes.** `schedule` is Celigo's cron string or JSON null / `""` (= on demand). Only the
  6-field shape `? <minutes> <hours> ? * *` is humanised (minute list or `*/N`; hour list, `*`,
  `0-23`, or `*/N`) and only that shape yields an **interval** for the stall check. Any other shape is
  shown verbatim and makes no stall claim.
- **Stalled?** (question mark mandatory): flow is on (`disabled` false), has an interval, has a
  `last_executed_at`, and `last_synced_at - last_executed_at > 2 × interval`. Missed runs =
  `floor(age / interval)`. Computed against the sync time, never the wall clock. Paused flows are
  never stalled. Flows with no schedule get no stall claim.
- **Schedule pill** per flow: `on time` | `stalled? N runs missed` | `on demand` | `paused` |
  `no run recorded` (when `last_executed_at` is null — never "never ran").
- **Error pill**: `N open errors · checked <sync age>` — the zero is a claim with a timestamp. Open =
  stored error rows with `resolved_at IS NULL AND purged_at IS NULL`. Celigo's own `numOpenError`
  (kept in the flow's raw object) is shown as a caption when it disagrees, never averaged.
- **Script clone state** on a hook/transform/filter chip: the attachment's script belongs to a family
  (`dedup_key`); `×N` = N copies with one content hash; `C/3` = this step runs the copy whose hash is
  the 3rd distinct hash by earliest `celigo_last_modified` (letters A, B, C… in that order), with an
  amber dot when the family has > 1 distinct hash (`content_diverged`).

## 3. Backend contract (all read-only routes stay behind the `celigo` feature flag and tenant scoping)

### 3.1 Migration + sync (T2): step names

- New nullable column `celigo_flow_steps.reference_name` (text).
- Phase D of the sync already fetches and sanitises each export/import; it now passes the object's
  `name` into the existing non-None-only backfill so a missing name never clobbers a stored one.
- `reference_name` is exposed on every step in the flow-detail response. A step whose name is null
  renders the honest fallback (see §4.3), never an invented name.

### 3.2 Flow detail `GET /celigo/flows/{flow_id}` additions

Per step: `reference_name`, `kind` (`source|lookup|destination`), `record_type`, `operation`,
`search_id`, `open_error_count`.
Per flow: `routers` projected from the synced flow object —
`[{id, name, route_records_to, route_records_using, has_script_slot, branches: [{id, name,
rule_count, next_router_id, order}]}]` (order = declared position; `rule_count` = number of
top-level entries in `inputFilter.rules`, 0 when absent) — plus `open_error_count`,
`celigo_open_error_count` (from `numOpenError`, null when absent) and `last_error_at`.
Routers with no step rows are still listed (an empty branch is a declared-but-empty lane).

### 3.3 Flow list `GET /celigo/integrations/{id}/flows` additions (per flow)

`step_count`, `router_count`, `branch_count`, `lookup_count`, `script_count`,
`diverged_family_count`, `writes: [{record_type, count}]`, `celigo_last_modified`,
`open_error_count`. One query set per request, not one per flow.

### 3.4 Integrations list `GET /celigo/integrations` additions (per integration, one request for the dashboard)

`flow_count`, `scheduled_count`, `on_demand_count`, `paused_count`, `step_count`, `router_count`,
`lookup_count`, `script_count`, `no_run_count`, `open_error_count`, `changes_since_sync`,
`last_run_at` (max `last_executed_at`), `writes: [{record_type, count}]`, `adaptor_families: [str]`,
`last_synced_at` (the connection's). The client no longer fans out one request per integration
for counts.

### 3.5 Errors and changes

- `GET /celigo/errors` gains optional `flow_id`, `step_id`, `status=open|resolved` filters and
  returns signatures grouped with per-step attribution
  (`{signature, source, code, message, count, first_seen_at, last_seen_at, retriable,
  purge_at, step_id, trace_keys[]}`); the existing tenant scoping, flag gate and tests are reused.
- `GET /celigo/integrations/{id}/changes` and `GET /celigo/flows/{id}/changes` list config-change
  rows newest first (empty today).

## 4. Frontend

### 4.1 URL scheme — one writer

```
/workspace?surface=celigo                                   integrations (tiles | list, ?view=list)
/workspace?surface=celigo&integration=<uuid>[&tab=flows|scripts|errors|changes]
/workspace?surface=celigo&flow=<uuid>[&step=<stepUuid>][&script=<scriptUuid>]
```
`surface` moves from React state into the URL. A single hook owns reads and writes of these
params; it clears `file`/`workspace` params when entering the Celigo surface and vice versa; the
existing file/workspace effect ignores Celigo params. Every transition is `router.push` except
bubble selection (`replace`). With `useFeature('celigo')` false, `?surface=celigo` renders Files.
Breadcrumb at every level; Back walks up; Esc clears the selection, a second Esc closes the
drawer; an unknown `flow`/`integration` id renders "This flow is not in the last sync." one level
up with a link, never a blank page.

### 4.2 Screens (match the mockup)

- **Integrations**: header stats line; filters All · Open errors · Stalled · All paused; tile ▦ /
  list ☰ toggle; tiles sorted attention-first (open errors, then stalled) then by flow count; each
  tile = error pill + schedule pill + adaptor medallions, name, counts line, writes rollup, foot
  (last run · scripts · changes). ⌘K opens a palette over integrations and flows (names only).
- **Integration page**: header (name, Production, medallions, counts, writes), tabs Flows · Scripts
  · Errors · Changes; Flows table columns Flow · Steps (◉→◇…→n glyph) · Writes · Schedule
  (humanised + cron verbatim) · Last run (relative to sync + pill) · Last updated · Errors ·
  Scripts (count + "N diverged") · State; grouped On/scheduled · On/on demand · Paused. Row click
  → flow page. Clicking an errors count opens a per-step drawer (step · errors · last open error).
  Scripts tab = the approved Scripts view scoped to the integration (its own follow-up if not yet
  built: show the existing script viewer list scoped by integration).
- **Flow page**: breadcrumb; header with pills, name, Copy link, Open in Celigo; facts line;
  derived one-sentence summary; Celigo AI description block captioned "inherited from the clone
  source" when the flow has a `source_id`; own `PanelGroup` (id `celigo-flow-v1`) with navigator
  rail (⌘B expands to the sibling-flows list with health dots) · canvas · inspector (300px).
- **Canvas**: deterministic layered left-to-right layout (never force-directed): ranks = sources ·
  [router → its single-branch lookups]* · router · branch lanes; lanes stacked by declared branch
  order; the same export/import in two branches is two bubbles; edges bubble→bubble with arrow
  markers; `proceed_on_failure` dashes the outgoing edge and labels it "continues on failure".
  Fit-to-width on load with +/fit controls; selection = accent ring mirrored in `&step=`.
  Chained routers (`next_router_id`) are drawn as a chain; >1 router without chain info fans out
  with a caption "router order unverified"; a declared-but-empty branch draws a faint "no steps"
  placeholder; a flow with 0 steps draws the sentence "no steps recorded".
- **Bubble** (212×162): eyebrow = kind · app family + red "N open" badge; title = `reference_name`
  or the fallback; fact line = adaptor kind · operation · record type / saved search id; affordance
  chips in Celigo's per-side order — source: transform · hooks · output filter; lookup: input filter
  · response mapping · hooks · transform; destination: input filter · NetSuite field mapping ·
  response mapping · hooks — with three states: configured (coloured), looked-and-none (hollow),
  cannot-say (dashed; the NetSuite field mapping chip is always dashed in this build); footer =
  `stops flow on failure` / `continues on failure` / `retries on|skipped`. Clicking a chip selects
  the bubble and opens the matching inspector tab.
- **Inspector**: tabs Facts · Filter · Mapping (labelled "response mapping") · Scripts · Errors;
  resting state (nothing selected) = flow Overview (header facts, adaptor mix, AI description, sync
  freshness). Scripts tab lists attachments with family state and "Open source →".
- **Script drawer**: the existing script viewer re-homed from a Dialog to a side panel over the
  inspector; inert highlighted source; shield copy "shown to you only, never sent to the assistant
  (N2)"; copies-in-family list with the current copy marked. The existing banner copy that promises
  quoting to the assistant is replaced everywhere it appears.
- **States**: quiet-today sentence with sync time in the Errors tab; stalled?; paused banner "This
  flow is Off in Celigo — mirrored here, not changeable here." with bubbles at 60%; no run
  recorded; no steps recorded; sync stale (toolbar pill amber past 2 h); never synced (counts "—");
  request failed (visibly distinct, Retry, header stays); unknown id; config changed (accent pill,
  orange ring on changed steps).

### 4.3 Fallback title when `reference_name` is null

NetSuite destination: `<operation> <record_type>` (e.g. `add salesorder`); NetSuite lookup:
`lookup <record_type> · search <search_id>`; HTTP/other: `<family> <kind> · name not synced`, muted.

## 5. Tests and verification

- Backend: TDD per task; DB tests on the local docker Postgres harness; the seed helpers in
  `backend/tests/api/test_celigo_flows_api.py` grow a router-chain world (2 routers, 3 branches,
  a lookup, hook attachments across 2 flows with 2 hashes) reused by every new test.
- Frontend: vitest unit tests for `schedule.ts` (the observed cron shapes; verbatim fallback), the
  layout (`layout.ts` fixture matrix: single source→destination · multi-source · top-level chain ·
  one router with 2 lanes · empty lane · duplicate step across branches · 0 steps · chained
  routers · 2 unchained routers), the route hook (single writer, clears foreign params, flag off);
  component tests per screen; a DOM-level page test that no deploy/push affordance is mounted on the
  Celigo branch; a test that script source never appears outside the drawer.
- e2e: seeded-tenant Playwright spec with a Celigo fixture (integration, flows with a router chain,
  attachments with bodies, one error signature across two steps) asserting the tile pill, table
  cell, header pill, bubble badge, navigator dot and Errors tab agree on the count.
- Gate: `code-review-multiangle` before merge; live smoke on staging (Framework tenant) after a
  resync populates `reference_name`: bubbles read the operator's names.

## 6. Non-goals and standing rules

No writes of any kind; no connections sync; no NetSuite field-mapping sync; no run history; no
Sync-now button; no script content to the assistant; no sandbox rows; migrations to the local
Docker DB only (never Supabase directly; staging auto-migrates on deploy); the sync stays off the
Beat schedule (manual trigger).
