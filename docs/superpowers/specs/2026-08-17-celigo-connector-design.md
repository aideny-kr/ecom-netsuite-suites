# Celigo Connector — Design Spec (Phases 1 + 2)

**Date:** 2026-08-17
**Branch:** `feat/celigo-mcp-connector`
**Tier:** T2 (secrets · migration · RLS · cron · prompt-pollution surface · MCP mutation writes)
**Status:** Approved for planning

---

## 1. Problem

Framework's NetSuite records are not produced by NetSuite. They are produced by **Celigo
integrator.io flows**, which run on schedules, apply filters, transform records through
tenant-written JavaScript, and fail silently into a 30-day error queue.

Our reconciliation engine matches Stripe charges against NetSuite customer deposits. The
deposits themselves are created by a Celigo flow — `NS - Create Customer Deposits (Paid
Orders)` (`60e499b5fa12b015d5a2167e`), every 30 minutes. When recon reports an unmatched
charge, the root cause is very often *inside Celigo*: the export filter excluded the order,
the subsidiary was missing from a hardcoded lookup map, or a hook script threw.

**Today that entire layer is invisible to the product.** The agent can see the effect
(a missing deposit) but never the cause.

### Evidence (live, read-only probe of Framework's account, 2026-08-17)

Ten sales orders failed on 2026-08-17 in `New Sales Order to NetSuite - Multi-Subsidiary`:

```
source:    "pre_save_page_hook"     code: "script_error"
message:   "TypeError: Cannot read properties of null (reading 'name')"
traceKey:  15822111, 15241110, 14847341, 13431048, …   ← source-system order IDs
purgeAt:   2026-09-16                                   ← Celigo deletes this in 30 days
```

Those orders never reached NetSuite → no sales order → no customer deposit → they appear in
recon as unmatched Stripe charges with no explanation. The cause is a null-guard bug in one
of *their* scripts, and it is knowable.

Account scale: **36 integrations, 50+ connections, 50+ scripts**; the `Solidus + NetSuite`
integration alone has 19 flows, 4 of which carry 32 open errors.

## 2. Goals / Non-goals

**Goals (this spec)**
- G1. Connect a Celigo account from Settings (both a REST row and an MCP row).
- G2. Sync and browse the integration map: Integrations → Flows → Steps → Scripts, with
      schedules, enabled state, connections, and open-error counts.
- G3. Surface script source, deduplicated across clones, with the flows that use it.
- G4. Snapshot flow errors before Celigo purges them at 30 days.
- G5. Attach a deterministic root-cause card to recon exceptions.
- G6. Expose Celigo's **read-only** MCP tools (`list_*`, `get_schema`, `search_knowledge_base`)
      to the chat agent, so it can answer "what does this flow do?" and "what is failing?"
      conversationally. Live tool calls only — no synced-corpus RAG (that is Phase 3, N2).

**Non-goals (deliberately deferred)**
- N1. Any Celigo **write** from our product (`upsert_flow`, `run_flow`, `patch_flow`,
      `delete_resource`, `triage_flow_errors`). Read-only, enforced mechanically.
- N2. Feeding script content into the RAG/knowledge layer (Phase 3).
- N3. Config drift/change alerting (Phase 5).
- N4. EDI, AI-agent, marketplace, storage, or user-management surfaces of the Celigo API.

## 3. Verified platform facts

All confirmed by live read-only calls, not documentation.

### 3.1 Object graph

```
Integration (_id, name, sandbox, mode?, description, lastModified)
  └─ Flow (_integrationId, disabled, schedule, timezone, lastExecutedAt, _flowGroupingId)
       ├─ pageGenerators[] → { _exportId, skipRetries }
       └─ pageProcessors[] → { type:"import"|"export", _importId|_exportId,
                               responseMapping, proceedOnFailure? }

Export / Import → _connectionId, adaptorType, _sourceId, sandbox
                  + script references (see 3.2)
Script          → content (raw JS, inline on GET by _id), _sourceId, sandbox
```

### 3.2 Script attachment is polymorphic — the recursive-walk requirement

Scripts do **not** attach at one predictable path. Confirmed live:

| Site | Shape |
|---|---|
| `transform.script` | `{_scriptId, function}` — **the most-used script in the account** |
| `hooks.*` | `preSavePage`, `preMap`, `postMap`, `postSubmit`, `postResponseMap`, … |
| `filter` | `type: "expression"` (declarative rules) **or** `type: "script"` |
| router branches | filter functions |

The initial documentation-derived recon asserted `hooks.preSavePage._scriptId` and would have
**missed the single most-used script in the account** (`transform.script._scriptId`, 20 clones).

→ **Design rule:** `graph.py` recursively walks the object tree collecting *every* `_scriptId`
occurrence with its JSON path. Do not enumerate Celigo's hook taxonomy.

### 3.3 Objects embed live secrets and PII — the allowlist requirement

Import `648a6cac060e6101e8372678` carries a captured production HTTP response in
`mockResponse`, including:

- `set-cookie`: `heroku-session-affinity=…`, `anonymous_id=…` for `.frame.work`
- customer order records, tracking numbers, full product/inventory data
- complete response headers and CSP

Similar payloads live in `mockOutput`, `sampleData`, `rawData`, `_headers`.

→ **Design rule:** the sanitizer is an **allowlist**. We copy only known-safe fields into our
database. A denylist breaks the day Celigo adds a field, and the failure mode is silently
storing session cookies. (Per `feedback_remove_the_possibility_not_the_instance`.)

### 3.4 `aiDescription` — Celigo already documents flows

Flows *and* imports/exports carry `aiDescription.summary` + `aiDescription.detailed` —
multi-paragraph prose describing sources, destinations, URIs, HTTP methods, field mappings,
and what transform scripts do. Coverage is uneven (`generatedOn: null`; one flow had it, a
sibling did not).

→ **Ingest it. Do not re-derive it.** Generate our own only where absent (Phase 3).

### 3.5 `_sourceId` — clone lineage, the dedup key

Cloned resources carry `_sourceId` pointing at their origin. `"FW Sales Order Hook"` appears
**~20 times** with distinct `_id`s; `"Dimerco 947"` ×3; `"Dimerco Router"` ×3. Without
grouping by `_sourceId` + content hash, the script list is unusable noise.

### 3.6 API mechanics

| Item | Value |
|---|---|
| Base URL | `https://api.integrator.io/v1` — **EU fully isolated at `api.eu.integrator.io`** |
| Auth | `Authorization: Bearer <token>` |
| Token kinds | **Service token** (admin, Custom scope = least privilege, durable) vs **PAT** (inherits user perms, **90-day auto-purge**) |
| Health check | `GET /v1/tokenInfo` |
| Rate limit | leaky bucket, 1,000 burst / 300 req/s, `Retry-After` on 429 |
| Pagination | opaque cursor (`nextCursor` + `hasMore`) |
| Projection | `include` / `exclude` comma-separated, **server-side** |
| Flow graph | `GET /v1/flows/{id}/descendants` returns every referenced step in one call |
| Scripts | list **omits** `content`; requires `GET /v1/scripts/{_id}` per script |
| Errors | per **step**, with `traceKey`, `errorId`, `retryDataKey`, `source`, `code`, `purgeAt` (30d) |
| Official MCP | `https://api.integrator.io/celigo-mcp`, Streamable HTTP, bearer or OAuth 2.1 |

**Sandbox is a per-resource boolean**, not a separate account — one token returns both
sandbox and production resources. Mixing rejects with 422.

**Two live-observed gotchas:**
- `list_flow_errors` with `_id` only returned `steps: []` for a flow that the integration-scoped
  summary showed had 12 errors. Use `_integrationId` summary mode, or `_id` + `_stepId`.
- `limit` was ignored in step mode (asked for 3, got 10).

## 4. Existing codebase shape

There is **no connector registry** — every integration is hand-rolled onto one of two models.
Zero `celigo` strings exist in the repo today.

| Anchor | Fact |
|---|---|
| `backend/app/models/connection.py:57` | `Connection` — REST/token style |
| `backend/app/schemas/connection.py:8` | provider regex `^(shopify\|stripe\|netsuite)$` — must extend |
| `backend/app/models/mcp_connector.py:17-37` | `McpConnector` — adds `server_url`, `discovered_tools`, `is_enabled` |
| `backend/app/schemas/mcp_connector.py:41-44` | provider regex + `auth_type` regex — must extend |
| `backend/app/core/encryption.py:8-30` | Fernet `encrypt_credentials` / `decrypt_credentials` |
| `backend/alembic/versions/092_user_dashboard_preference.py:27-55` | **the** FORCE-RLS migration template |
| `backend/alembic/versions/093_recon_reject_labels.py` | current head; re-parent, never merge-migrate |
| `backend/app/workers/base_task.py:16-142` | `InstrumentedTask` |
| `backend/app/services/ingestion/base.py:23-63` | freshness-cursor helper |
| `backend/app/models/connection.py:43` | `DISPATCHABLE_CONNECTION_STATUSES` includes `'error'` on purpose (2026-07-29 silent-skip incident) |
| `frontend/src/app/(dashboard)/settings/page.tsx:2858-2905` | admin-gated section block |
| `frontend/src/components/settings/stripe-connector-card.tsx:79` | token-paste card template |
| `frontend/src/components/settings/bigquery-connection-section.tsx:55-320` | `BigQueryTableSelector` — discover→tree→persist, the flow-mapper analog |
| `frontend/src/lib/api-client.ts:294-339` | never raw `fetch` |
| `backend/app/services/chat/mutation_guard.py:21-26` | **`_MUTATION_TOOL_NAMES` hardcodes 4 NetSuite verbs** |

> `connections` and `mcp_connectors` are RLS-enabled but **not FORCE'd**, and
> `connection_alerts` has no RLS at all. With a BYPASSRLS worker role these are effectively
> unisolated for workers. New Celigo tables must not join that pile.

## 5. Architecture

### 5.1 Two connector rows

| Row | Provider | Purpose | Auth |
|---|---|---|---|
| `Connection` | `celigo` | REST sync → our tables → Settings UI → recon link | Custom-scoped **service token** |
| `McpConnector` | `celigo_mcp` | Chat agent tools | bearer, `server_url=https://api.integrator.io/celigo-mcp` |

`metadata_json` (REST row, unencrypted): `{region: "us"|"eu", account_name, environment_scope}`.
Token → `encrypted_credentials` (Fernet).

**Service token, not PAT.** PATs auto-purge at 90 days — the identical silent-death failure
mode as `reference_netsuite_oauth_single_use_token_and_recon_freshness_cursor`, where a
connection flipped to `error`, sync froze, and nothing alerted. Health-check `GET /v1/tokenInfo`
on the existing 15-min `connection_health` cycle.

### 5.2 Backend package `backend/app/services/celigo/`

| Module | Responsibility |
|---|---|
| `client.py` | httpx; region-aware base URL; cursor pagination; `include`/`exclude` projection; 429 `Retry-After`; 401 returns `{message}` not `{errors:[]}` (parser gotcha) |
| `sanitizer.py` | **allowlist** field copier (§3.3) |
| `graph.py` | recursive `_scriptId` walk → `(script_id, function, json_path, site_type)` (§3.2) |
| `sync_service.py` | orchestrates sync, freshness cursor, error snapshotting |
| `provenance.py` | Phase 2 — flow→NetSuite-record derivation + filter evaluation |

Projection is a **security control, not an optimization**: `exclude` means the cookie- and
PII-bearing fields are never fetched over the wire in the first place. The sanitizer is
defence in depth behind it.

### 5.3 Data model — 6 tables, all `ENABLE` + `FORCE ROW LEVEL SECURITY`

Per the `092` template, `get_current_tenant_id()` in both `USING` and `WITH CHECK`.

| Table | Notes |
|---|---|
| `celigo_integrations` | `celigo_id`, `name`, `sandbox`, `mode`, `description`, `last_modified` |
| `celigo_flows` | `_integrationId` FK, `disabled`, `schedule`, `timezone`, `last_executed_at`, `ai_description_summary/detailed`, `flow_grouping_id`, `source_id` |
| `celigo_flow_steps` | exports + imports unified; `role` ∈ `generator`/`processor`, `adaptor_type`, `connection_celigo_id`, `filter_json`, `mapping_json`, `ai_description_*` |
| `celigo_scripts` | `content`, `content_hash`, `source_id`, `sandbox` |
| `celigo_script_attachments` | `flow_step_id` → `script_id`, plus `function_name`, `json_path`, `site_type` |
| `celigo_flow_errors` | **snapshot**: `trace_key`, `error_id`, `source`, `code`, `message`, `occurred_at`, `purge_at`, `flow_job_id`, `retriable` |

Three rationales:
- **Errors are snapshotted, not proxied** — `purgeAt` destroys the source at 30 days. This table
  *is* the audit trail.
- **`celigo_script_attachments` is its own table** — the edge carries data
  (`function_name`, `json_path`), and one script exposes several functions at several sites.
- **`celigo_scripts` dedups on `(source_id, content_hash)`** — 20 clones of one script otherwise.

All tables carry `tenant_id` + `celigo_connection_id`. Unique constraint on
`(tenant_id, celigo_connection_id, celigo_id)`.

### 5.4 Sync worker

`InstrumentedTask`, nightly Beat + manual "Sync now". Inherits the
dispatch-even-on-`error`-status posture (`DISPATCHABLE_CONNECTION_STATUSES`) and the
freshness-cursor discipline from `services/ingestion/base.py`. Errors are re-snapshotted every
run; existing rows are never deleted on purge.

**Sequencing:** integrations → flows (`/descendants` for the step graph) → scripts by id
(N+1, fine at 300 req/s) → per-step errors via `_integrationId` summary then `_id`+`_stepId`.

### 5.5 Frontend

`frontend/src/components/settings/celigo-section.tsx`, rendered in the `isAdmin` block of
`settings/page.tsx`, wrapped in `SectionErrorBoundary`, gated on a default-off
`celigo` feature flag.

- Connect card ← `stripe-connector-card.tsx` (fields → test → connect → status).
- Flow map ← `BigQueryTableSelector` (discover → tree → persist). Tree:
  Integration → Flow → Steps → attached Scripts, with schedule, enabled state, open-error count.
- Script viewer: source + "used by N flows" (deduped), with the attachment site for each.
- Data layer: `frontend/src/hooks/use-celigo.ts`, TanStack Query over `apiClient`. Never raw `fetch`.

**Approved mockup (acceptance reference):**
<https://claude.ai/code/artifact/0c482ad7-6e46-461c-969f-711221e7c69f> — five screens
(connect · flow map · flow detail · script · recon root cause), rendered against the real
design tokens and populated with live account data. Approved 2026-08-18. Acceptance for the
frontend work is "matches this artifact", not merely green tests.

**Naming — user-facing vocabulary is not Celigo's vocabulary.** Fixed here so it is not
re-litigated per component:

| Celigo term | We say | Why |
|---|---|---|
| `pageGenerator` | **Source** | Nobody outside Celigo knows what a page generator is |
| `pageProcessor` | **Destination** (or **Lookup** when `type: "export"`) | Describes the job, not the engine |
| `disabled: true` | **Paused** | Reversible state, not a broken one |
| `numOpenError` | **Open errors** | Already the term used in recon |
| `aiDescription` | **Summary** | Its origin is an implementation detail |
| script `content` | **Source** (code) | Shown read-only; never "run" or "execute" in UI copy |

Screen 02 leads with open-error count because that is what brings someone to the page; scripts
are deduplicated before render (§3.5). Screen 04 must display the **attachment site** per usage
(`transform.script` vs `hooks.preSavePage`) — the mockup shows one script attached both ways,
which is the visible proof of why §3.2 walks recursively.

### 5.6 Chat-side wiring (G6)

The generic external-MCP path already works: `mcp_client_service._build_headers()` handles
`bearer`, `discover_tools()` populates `McpConnector.discovered_tools`, and tools surface to
the agent as `ext__<connector_hex>__<tool>` (`chat/tools.py:111-184`). Four gaps to close:

| Gap | Anchor | Action |
|---|---|---|
| Read-only allowlist | `build_external_tool_definitions` | Filter to `list_*`, `get_schema`, `search_knowledge_base`. **This is the write-safety control** (§7.1) |
| Provider description | `unified_agent.py:34-48` `_PROVIDER_DESCRIPTIONS` | Add a `celigo_mcp` entry; `custom` falls back to a bare label |
| Execution guidance | `tool_inventory.py:58-185` | Pattern-matches NetSuite names only; Celigo tools otherwise land in an unguided "other" bucket |
| Timeout | `mcp_client_service.py:216-221` | Default is 15s; account-wide Celigo `list_*` calls can exceed it. Raise per-provider |

Two known weaknesses of this path, accepted for Phase 1 and recorded rather than silently
inherited: `_execute_external_tool` (`chat/tools.py:324-350`) bypasses `governed_execute` and
rate limiting, and `discovered_tools` refreshes only on create/reauth/test, never on a
schedule — so a Celigo tool-catalog change goes unnoticed until someone re-tests.

Tool inventory changes are prompt-pollution surface: the `{{TOOL_INVENTORY}}` placeholder
convention holds (never hardcode tool names), and the vs-Claude+MCP benchmark gate applies.

## 6. Phase 2 — recon root-cause link

Deterministic. No LLM in the critical path.

1. **Which flows write which records** — derived from synced import config:
   `netsuite_da.recordType == "customerdeposit"` ∧ `operation == "add"`.
   Config-derived, not inferred. Stored in `provenance.py` output.
2. **Why a specific record is absent** — evaluate the export's stored `filter` expression
   against the order. The deposits export filter is literally
   `New Deposit Amount > 0 AND lowercase(payment_method) NOT CONTAINS "balance"` —
   a machine-checkable exclusion reason.
3. **Whether it errored** — join `celigo_flow_errors.trace_key` to the recon exception's
   order reference.

Recon exception rows gain a root-cause card:

> **No customer deposit.** The Celigo flow `NS - Create Customer Deposits (Paid Orders)`
> should have created it. Upstream flow `New Sales Order to NetSuite` failed for this order
> on 2026-08-17: `TypeError: Cannot read properties of null (reading 'name')` in
> `pre_save_page_hook` → script `FW Sales Order Hook`.

**Open risk to validate during implementation:** `traceKey` is a source-system order id
(Solidus). The join to our recon order ref must be verified against real data before this
card is trusted — if the key spaces differ, the card degrades to flow-level
("this flow has 10 open errors today") rather than order-level. Do not ship an order-level
claim that has not been verified end-to-end on live data.

## 7. Security

### 7.1 `mutation_guard` generalization — blocking prerequisite

`_MUTATION_TOOL_NAMES` (`mutation_guard.py:21-26`) hardcodes 4 NetSuite verbs.
`classify_mutation()` returns `None` for `upsert_flow`, `run_flow`, `patch_flow`,
`delete_resource`, `triage_flow_errors`, `manage_user` — meaning **every Celigo write tool
would auto-execute with no HITL** (`base_agent.py:1234-1341`). This violates CLAUDE.md
mistake #2.

Because both rows ship on day one, this generalization **lands first, in the same PR, before
the MCP row is enabled.**

Writes are made **unreachable, not discouraged**: a read-only allowlist filters tool
definitions at `build_external_tool_definitions` time, so write tools never enter the model's
inventory. Prompt-level instruction is not a control — Celigo's own `delete_resource` never
blocks server-side, and the "remove the possibility, not the instance" rule applies.

Defence in depth: request a **Custom-scoped** service token so the credential itself lacks
write permission.

### 7.2 Untrusted content

Celigo script `content` is **arbitrary JavaScript written by the tenant's integrators** and
`aiDescription` is **LLM-generated text from a third-party system**. Both are prompt-injection
channels. They enter agent context only inside a delimited untrusted block, are never treated
as instructions, and are never executed.

### 7.3 Data protection

The §3.3 allowlist is the control that keeps live session cookies and customer PII out of our
database. It is not optional and it is not a denylist.

## 8. Testing

TDD — failing test first, proven red against broken code.

| Target | Test |
|---|---|
| `sanitizer.py` | Fixture built from the **real** cookie-bearing `mockResponse` (redacted). Asserts no `set-cookie`, no `_headers`, no customer records survive. Asserts an *unknown new field* is dropped by default. |
| `graph.py` | Fixtures for `transform.script`, `hooks.preSavePage`, `filter.type=script`, and a router branch — all four must be found. Regression fixture = the real export that broke the recon's inference. |
| `client.py` | Cursor pagination, 429 `Retry-After` honoured, EU region routing, 401 `{message}` envelope parsed. |
| `provenance.py` | Filter evaluation: an order with `payment_method="balance"` yields the exclusion reason. |
| RLS | Seeded-tenant e2e: tenant B cannot read tenant A's Celigo rows, including as the BYPASSRLS worker role. |
| `mutation_guard` | Every Celigo write verb classifies as a mutation; write tools absent from built definitions. |
| Frontend | Vitest + RTL per `sheets-connector-card.test.tsx:1-80`. |

Live smoke: **read-only**, `uat-smoke` tenant only, never a real tenant.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Cookie/PII ingestion | `exclude` projection + allowlist sanitizer + explicit test |
| Celigo write auto-execution | `mutation_guard` generalization lands first; allowlist at definition build; Custom-scoped token |
| Prompt injection via script JS / `aiDescription` | Delimited untrusted block; never instructions |
| Token silent death | Service token not PAT; `/v1/tokenInfo` on the 15-min health cycle; alert on flip to `error` |
| `traceKey` ≠ our order ref | Verify on live data before shipping order-level claims; degrade to flow-level |
| RLS gap | `092` FORCE pattern; explicit BYPASSRLS-role test |
| Benchmark regression | MCP row touches tool inventory → vs-Claude+MCP benchmark gate applies |
| Sync cost at scale | 36 integrations × ~19 flows + N+1 scripts, well inside 300 req/s |

## 10. Decisions (chose X over Y because ___)

- **Both connector rows day one** over REST-first — user decision, 2026-08-17. Accepted cost:
  `mutation_guard` generalization moves into Phase 1 as a blocking prerequisite.
- **Phases 1+2 in one spec** over Phase 1 alone — ships something that changes how close/recon
  works, not just a settings page.
- **Service token** over PAT — 90-day auto-purge is a known silent-death mode we have already
  been bitten by.
- **Snapshot errors** over live proxy — `purgeAt` destroys evidence at 30 days.
- **Recursive `_scriptId` walk** over enumerated hook paths — enumeration provably missed the
  most-used script in the account.
- **Allowlist sanitizer** over denylist — denylist fails open on new Celigo fields, and the
  payload demonstrably contains live session cookies.
- **Deterministic provenance** over LLM inference — filters and record types are config; config
  is checkable.
- **Named provider** (`celigo` / `celigo_mcp`) over riding `provider="custom"` — region,
  sandbox, tokenInfo health, and the mapper entry point all need to branch on it.

## 11. Out of scope / DON'T

- **DON'T** call `triage_flow_errors`, `run_flow`, `upsert_*`, `patch_flow`, or
  `delete_resource` from any code path in this spec.
- **DON'T** feed script `content` into knowledge profiles or RAG (Phase 3).
- **DON'T** add these tables without FORCE RLS.
- **DON'T** use a merge migration to resolve parallel heads — re-parent.
