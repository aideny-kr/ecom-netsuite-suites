# Celigo Plan B — Flow Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync a tenant's Celigo integrations, flows, steps, scripts and errors into our own tables — sanitized, signature-grouped, and preserved past Celigo's 30-day purge — and let an admin browse them in Settings.

**Architecture:** A read-only REST sync into six FORCE-RLS tables. An allowlist sanitizer keeps captured production payloads (which demonstrably contain live session cookies) out of the database. A recursive `_scriptId` walk maps scripts to the steps that use them. Errors are append-and-preserve: rows are never deleted when Celigo purges them.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 (`Mapped[]` + `mapped_column()`) · Alembic · Celery (`InstrumentedTask`) · Next.js 14 · TanStack Query · Vitest

**Spec:** `docs/superpowers/specs/2026-08-25-celigo-flow-map-design.md`
**Depends on:** PR #202 merged (connection, client, `celigo_tool_policy`, write guard)
**Mockup (frontend acceptance):** <https://claude.ai/code/artifact/0c482ad7-6e46-461c-969f-711221e7c69f> screens 02-04

---

## Global Constraints

- **Worktree:** work only in the worktree you are given; never `cd` to the main checkout.
- **pytest** (worktrees have no `.venv`): `cd <worktree>/backend` then
  `/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/<file> -v`.
  Confirm you are testing the right tree: `python -c "import app.services.celigo.client as c; print(c.__file__)"` must print a path under your worktree.
- **Frontend:** `npx vitest run`, `npx tsc --noEmit` from `<worktree>/frontend`.
  `@testing-library/user-event` is NOT a dependency — use `fireEvent`.
- **E2BIG:** if Bash fails with `E2BIG`, retry with `dangerouslyDisableSandbox: true`.
- **TDD mandatory.** Failing test first, observed red *for the right reason*, then implement.
- **The briefs in this plan have been wrong before.** Five defects in Plan A came from test code written against unverified signatures. Treat code blocks here as intent; verify fixtures, constructor signatures and route prefixes against the real codebase first and **report every deviation** rather than forcing broken code to run.
- **READ-ONLY.** No Celigo write call anywhere: no `retry`, `resolve`, `upsert_*`, `run_flow`, `patch_flow`, `delete_resource`, `triage_flow_errors`. That is PR #204's track.
- **Never delete error rows** when they vanish from Celigo — surviving the purge is the point.
- **Never log error `message` text** — messages contain PII (`deleted_user_*@user.deleted`, customer emails).
- New tables get `ENABLE` **and** `FORCE ROW LEVEL SECURITY` per `092_user_dashboard_preference.py`.
- **`retriable` is not a retry-success signal.** Store it; never branch on it as if it meant "retry will work".
- SQLAlchemy 2.0 only (`Mapped[]` + `mapped_column()`); `Annotated[Type, Depends(...)]`, never bare `Depends()`.
- Audit mutations via `audit_service.log_event()`, then `await db.commit()`.
- Commit per task. Never amend, never force-push.
- **Tier T2** — migration, RLS, cron, PII-bearing ingestion. Blocking multi-angle gate pre-merge.

## Verified Facts (probed live — do not re-derive)

| Fact | Consequence for this plan |
|---|---|
| Errors carry `purgeAt` ≈ 30 days after `occurredAt` | Task 6 must preserve, never delete |
| `list_flow_errors` with `_id` alone returns `steps: []` even when errors exist | Always use `_integrationId` summary, then `_id` + `_stepId` |
| `limit` is ignored in step mode | Do not rely on it for paging |
| Errors are per **step**; one flow can have several failing steps | Schema keys errors to a step, not a flow |
| All 30 live errors are `retriable: true` and all are deterministic | Store the flag, never branch on it |
| `_sourceId` marks clone lineage; one script has ~20 clones | Dedup on `(source_id, content_hash)` |
| Scripts attach at `transform.script`, `hooks.*`, `filter`, router branches | Recursive walk, not an enumerated hook list |
| `aiDescription{summary,detailed}` exists on flows and steps, coverage uneven | Ingest it; do not re-derive |
| Objects embed `mockResponse`/`mockOutput`/`sampleData`/`rawData`/`_headers` containing live `set-cookie` and customer PII | Allowlist sanitizer + `exclude` projection |
| **UNVERIFIED:** `GET /v1/flows/{id}/descendants` | Task 3 must confirm live before designing around it |

---

## Task 1: Sanitizer (allowlist)

**Files:** create `backend/app/services/celigo/sanitizer.py`; test `backend/tests/test_celigo_sanitizer.py`

**Interfaces:** `sanitize(resource_kind: str, raw: dict) -> dict` — returns a new dict containing only allowlisted keys for that kind. Unknown keys are dropped silently.

- [ ] **Step 1: Write the failing test.** Build the fixture from a REAL captured payload shape — an import object whose `mockResponse` contains `_headers` with `set-cookie`, a customer record, and product data. Assert:
  - no `set-cookie` value survives anywhere in the output (walk the whole structure, not just top level)
  - `mockResponse`, `mockOutput`, `sampleData`, `rawData`, `_headers` are absent
  - **an unknown, never-before-seen key is dropped by default** — this is the allowlist property; a denylist would pass it
  - allowlisted fields (`_id`, `name`, `adaptorType`, `_connectionId`, `_sourceId`, `sandbox`) survive
- [ ] **Step 2: Run — confirm RED** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement.** Per-kind allowlists for `integration`, `flow`, `export`, `import`, `script`, `error`. Recursive: nested dicts are filtered by their own allowlist, not passed through whole.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** — `feat(celigo): allowlist sanitizer keeps captured production payloads out of the database`

## Task 2: Script graph walker

**Files:** create `backend/app/services/celigo/graph.py`; test `backend/tests/test_celigo_graph.py`

**Interfaces:** `walk_script_refs(obj: dict) -> list[ScriptRef]` where `ScriptRef` carries `script_id`, `function_name | None`, `json_path`, `site_type`.

- [ ] **Step 1: Failing test** with four fixtures, each a real shape:
  - `transform: {type: "script", script: {_scriptId, function}}` — **the regression case**; the most-used script in the live account attaches here, and a hooks-only search misses it
  - `hooks: {preSavePage: {_scriptId, function}}`
  - `filter: {type: "script", script: {_scriptId}}` (note `filter` can also be `type: "expression"` — that must yield no ref)
  - a router branch containing a `_scriptId`
  Plus: an object with **no** script refs yields `[]`, and a deeply nested unknown structure containing `_scriptId` is still found.
- [ ] **Step 2: Run — confirm RED.**
- [ ] **Step 3: Implement** a recursive walk collecting every `_scriptId` occurrence with its JSON path. Do NOT enumerate Celigo's hook taxonomy — a new hook type must be found automatically.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** — `feat(celigo): recursive script-reference walk finds every attachment site`

## Task 3: Client extension — pagination, projection, fetchers

**Files:** modify `backend/app/services/celigo/client.py`; test `backend/tests/test_celigo_client.py`

**Interfaces:** `list_resource(kind, *, token, region, include=None, exclude=None, params=None, client=None) -> AsyncIterator[dict]` (transparently follows cursors); `get_resource(kind, celigo_id, ...) -> dict`; `list_flow_errors_for_step(flow_id, step_id, ...) -> list[dict]`; `list_error_summary_for_integration(integration_id, ...) -> dict`.

- [ ] **Step 1: VERIFY `/descendants` LIVE FIRST.** The spec marks it unverified. Make one real call against a known flow id and record the actual response shape in your report. If it does not return every referenced step in one call, design around per-id fetches instead and say so. **Do not build on it unverified.**
- [ ] **Step 2: Failing tests** using `httpx.MockTransport` (already the convention in this file): cursor pagination yields across pages and stops; `include`/`exclude` reach the query string; a 429 with `Retry-After` is honoured; `_integrationId` summary vs `_id`+`_stepId` are used for errors and `_id`-alone is never used.
- [ ] **Step 3: Run — confirm RED.**
- [ ] **Step 4: Implement.** Always pass `exclude` for the payload-bearing fields — keeping them off the wire is the first line of defence, the sanitizer is the second.
- [ ] **Step 5: GREEN**, then the full `test_celigo_client.py`.
- [ ] **Step 6: Commit** — `feat(celigo): paginated, projected resource fetchers`

## Task 4: Migration — seven tables, FORCE RLS

**Files:** create `backend/alembic/versions/<next>_celigo_flow_map.py`; test `backend/tests/test_celigo_flow_map_rls.py`

Tables: `celigo_integrations`, `celigo_flows`, `celigo_flow_steps`, `celigo_scripts`, `celigo_script_attachments`, `celigo_flow_errors`, `celigo_error_signatures` (+ `celigo_config_changes` in Task 7).

- [ ] **Step 1: Read `092_user_dashboard_preference.py:27-55` in full** — it is the FORCE-RLS template. Also read the current head and **re-parent onto it; never create a merge migration** (`093`'s docstring explains why).
- [ ] **Step 2: Failing RLS test** — tenant B cannot read tenant A's rows in any of the seven tables, **including as the BYPASSRLS worker role**. This is the test that matters; `ENABLE` without `FORCE` passes a naive check and fails this one.
- [ ] **Step 3: Run — confirm RED** (tables don't exist).
- [ ] **Step 4: Write the migration.** `ENABLE` **and** `FORCE ROW LEVEL SECURITY`, `get_current_tenant_id()` in both `USING` and `WITH CHECK`. Unique on `(tenant_id, celigo_connection_id, celigo_id)`. Index `celigo_flow_errors(tenant_id, trace_key)` — Plan C joins on it.
- [ ] **Step 5: GREEN.** Apply to BOTH databases (Supabase via `.venv/bin/alembic`, local via `docker exec … alembic upgrade head`) and confirm `downgrade -1` works.
- [ ] **Step 6: Commit** — `feat(celigo): flow-map tables with FORCE row-level security`

## Task 5: Models + repository

**Files:** create `backend/app/models/celigo.py`; create `backend/app/services/celigo/repository.py`; test `backend/tests/test_celigo_repository.py`

- [ ] **Step 1: Failing test** — upsert is idempotent (syncing the same payload twice yields one row, no duplicate-key error); `(source_id, content_hash)` dedup collapses ~20 clones of one script to a single logical script with N attachments.
- [ ] **Step 2: RED.** **Step 3:** implement. **Step 4:** GREEN.
- [ ] **Step 5: Commit** — `feat(celigo): flow-map models and idempotent repository`

## Task 6: Error snapshotting + signature grouping

**This is the task the whole plan exists for.** Errors observed on 2026-08-10 are still open and will be destroyed by Celigo on 2026-09-09.

**Files:** create `backend/app/services/celigo/errors.py`; test `backend/tests/test_celigo_error_signatures.py`

**Interfaces:** `fingerprint(source: str, code: str, message: str) -> str`; `upsert_errors(db, tenant_id, connection_id, step, raw_errors) -> None`

- [ ] **Step 1: Failing test** built from the REAL corpus (use these verbatim):
  - four `MISSING_SHIP_ADDRESS` messages differing only in order ref (`R694979090`, `R592677045`, `R797937775`, `R643957736`) and `deleted_user_*@user.deleted` ⇒ **one** signature, `occurrence_count == 4`
  - two `value_lookup_failed` messages differing only in email (`mjj@cfnson.com`, `jackplumer27@gmail.com`) ⇒ **one** signature
  - `TypeError: Cannot read properties of null (reading 'name')` ⇒ its **own** signature, distinct from the above
  - **the fingerprint contains no email address and no order ref** (PII must not leak into the grouping key)
  - **preservation:** an error present in a previous sync but absent from the current one is NOT deleted — it is marked resolved/purged and its row survives
- [ ] **Step 2: Run — confirm RED.**
- [ ] **Step 3: Implement.** Conservative normalisation: digits, emails, `R\d+` order refs, UUIDs, ISO timestamps. Store a sample message per signature. **Start narrow** — over-normalising merges genuinely different failures.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Prove the grouping on the real numbers** — 30 errors across 4 flows must collapse to roughly 3 signatures. Record the actual count in your report; if it is not ~3, the normaliser is wrong in one direction and you must say which.
- [ ] **Step 6: Commit** — `feat(celigo): snapshot flow errors and group them by signature`

## Task 7: Sync worker + drift detection

**Files:** create `backend/app/services/celigo/sync_service.py`; modify the Beat schedule; migration for `celigo_config_changes`; test `backend/tests/test_celigo_sync.py`

- [ ] **Step 1: Read an existing `InstrumentedTask` sync end-to-end first** (a NetSuite or Stripe worker) — task registration, tenant iteration, freshness cursor, error handling. Follow it; do not invent a new shape.
- [ ] **Step 2: Failing tests** — sequencing (integrations → flows → steps → scripts → errors per step); the freshness cursor advances only on success; a connection in `status="error"` is still dispatched (per `DISPATCHABLE_CONNECTION_STATUSES`, after the 2026-07-29 silent-skip incident); drift on `disabled`/`schedule`/`mapping_json`/`filter_json`/`content_hash` appends a `celigo_config_changes` row. Use the real observed drift as a fixture: `Balance Users to NetSuite` went `disabled: true` → `false` between 2026-08-17 and 2026-08-25.
- [ ] **Step 3: RED.** **Step 4:** implement. **Step 5:** GREEN.
- [ ] **Step 6: Commit** — `feat(celigo): nightly flow-map sync with drift detection`

## Task 8: Read APIs

**Files:** create `backend/app/api/v1/celigo_flows.py`; register in `router.py`; test `backend/tests/api/test_celigo_flows_api.py`

Endpoints (all `connections.view` + `require_feature("celigo")` — Plan A established the flag must gate the API, not just the UI):
`GET /celigo/integrations`, `GET /celigo/integrations/{id}/flows`, `GET /celigo/flows/{id}` (steps + attachments), `GET /celigo/scripts/{id}` (content + used-by), `GET /celigo/errors?signature=…`

- [ ] **Step 1: Recon the real fixtures first** — `admin_user` returns a `(User, headers)` tuple; the fixture is `db`, not `db_session`. Confirm before writing.
- [ ] **Step 2: Failing tests** including tenant isolation on every endpoint and a 403 when the flag is off. **Step 3:** RED. **Step 4:** implement. **Step 5:** GREEN.
- [ ] **Step 6: Commit** — `feat(celigo): read APIs for the flow map`

## Task 9: Flow map + flow detail UI

**Files:** create `frontend/src/hooks/use-celigo-flows.ts`, `frontend/src/components/settings/celigo-flow-map.tsx`; modify the Celigo settings section; tests colocated in `__tests__/`

Mockup screens 02-03. **Two deviations from the mockup, both driven by live data:**
- the error column shows **signature count** ("3 root causes") above raw count ("30 errors")
- paused flows stay visible and clearly marked, not filtered out

- [ ] **Step 1: Read `bigquery-connection-section.tsx`'s tree UI in full** — the proven discover→tree→persist pattern. **Step 2:** failing tests. **Step 3:** RED. **Step 4:** implement. **Step 5:** `npx vitest run` + `npx tsc --noEmit`.
- [ ] **Step 6: Commit** — `feat(celigo): flow map and flow detail in Settings`

## Task 10: Script viewer

**Files:** create `frontend/src/components/settings/celigo-script-viewer.tsx`; test colocated

Mockup screen 04. Deduplicated by `(source_id, content_hash)`, showing every attachment site with its `function_name` and `json_path`.

- [ ] **Step 1: Failing test** — one script attached at both `transform.script` and `hooks.preSavePage` renders **both** sites; ~20 clones collapse to one entry.
- [ ] **Step 2: RED.** **Step 3:** implement — script content renders inside the untrusted-content block (arbitrary tenant JavaScript, never instructions). **Step 4:** GREEN + tsc.
- [ ] **Step 5: Commit** — `feat(celigo): script viewer with deduplicated attachment sites`

## Task 11: Provenance derivation (Plan C's input)

**Files:** create `backend/app/services/celigo/provenance.py`; test `backend/tests/test_celigo_provenance.py`

- [ ] **Step 1: Failing test** — from synced import config, derive which flows write which NetSuite record types via `netsuite_da.recordType` + `operation`. Config-derived, not inferred.
  **Use the corrected chain:** the deposit flows are currently clean; the failures are at the sales-order flows. A test asserting "deposit flow errors explain unmatched charges" would encode the wrong model.
- [ ] **Step 2: RED.** **Step 3:** implement. **Step 4:** GREEN.
- [ ] **Step 5: Commit** — `feat(celigo): derive flow→NetSuite-record provenance`

## Task 12: Regression gate

- [ ] **Step 1:** `./scripts/verify.sh` from the worktree ROOT (not `backend/` — it exits 127), backgrounded. **Run only one at a time** — concurrent runs collide on `$TMPDIR/verify.$PID` and the baseline leg silently fails while still printing PASS.
- [ ] **Step 2:** Read verify.sh's own PASS/FAIL line AND confirm the `baseline:` line is **non-empty**. A blank baseline with a PASS verdict is a false pass.
- [ ] **Step 3:** Confirm the delta equals exactly the tests this plan added.
- [ ] **Step 4:** If `test_put_refreshes_intent_embedding_on_text_change` fails, it is the known ~1/1000 `hash() % 1000` flake — re-run, never edit that file.
- [ ] **Step 5: Executable proof** that no Celigo write call exists anywhere in the new code:
  ```bash
  grep -rnE 'retry|resolve|upsert_|run_flow|patch_flow|delete_resource|triage_flow_errors' \
    backend/app/services/celigo/ | grep -v 'retry_data_key\|Retry-After\|retriable\|resolved_at' \
    && echo "REVIEW THESE" || echo "PASS: no write verbs in the sync path"
  ```

---

## Self-Review

**Spec coverage.** Tasks 1-12 implement spec §4 (all six tables + signatures), §4.2 (grouping), §4.3 (sanitizer), §4.4 (sync), §4.5 (drift), §4.6 (provenance), §5 (UI). Goals G1-G6 all have a task.

Deliberately **not** here: any write path (spec N1, PR #204's track), script content into RAG (N2), alerting (N3), the recon card itself (N4, Plan C).

**Placeholder scan.** None. Tasks 3, 7, 8, 9 open with a "read the real thing first" step rather than reproducing files not yet read — a deliberate instruction to inspect named files.

**Type consistency.** `sanitize(resource_kind, raw)`, `walk_script_refs(obj) -> list[ScriptRef]`, `fingerprint(source, code, message)` are used identically wherever they appear.

**Known unknown, stated not hidden.** `/v1/flows/{id}/descendants` is unverified. Task 3 Step 1 verifies it live before anything is built on it, and mandates a per-id fallback. This is deliberate — the previous plan shipped five defects from exactly this kind of unchecked assumption.
