# Celigo Flow Map — Design Spec (Plan B)

**Date:** 2026-08-25
**Status:** Approved for planning
**Depends on:** PR #202 (`feat/celigo-mcp-connector`) — connect + read-only guards + write guard
**Sibling track:** PR #204 (write capability / resolve-retry). Independent of this.
**Tier:** T2 (migration · RLS/tenant-scoping · cron · secrets-adjacent ingestion · prompt-pollution surface)

---

## 1. Problem

Plan A connects a Celigo account. It surfaces nothing. Today a tenant can paste a token
and see "Connected" — and learn nothing else from the product.

Meanwhile, probed live on 2026-08-25 against the real `Solidus + NetSuite` integration
(19 flows): **30 open errors across 4 flows**, in three distinct classes, none of them
visible anywhere in our app.

### The evidence that sets this spec's priorities

**Errors are being destroyed on a 30-day timer.**
Two errors carrying trace keys `10713483` and `10711331` were observed on **2026-08-17**
and are still open on **2026-08-25** — first seen `2026-08-10`. Celigo purges them on
`2026-09-09`. A production defect that has been failing for three weeks will have its
evidence deleted, and nothing outside Celigo will remember it happened.

**Error messages are already diagnostic.** The integrators have invested in them:
> `MISSING_SHIP_ADDRESS: order R694979090 has no ship_address in the Solidus payload, so
> it cannot be mapped to NetSuite. The page was NOT exported. Usually a GDPR-scrubbed
> customer (email deleted_user_242530@user.deleted) — check the order in Solidus.`

Thirty errors collapse to **three root causes**: erased-customer data, duplicate NetSuite
customer records (`value_lookup_failed: More than one match for ["email","is",...] for the
record type "Customer"`), and one null-guard script bug. Grouping by signature gets you
that with no model in the path.

**Config drifts silently.** `Balance Users to NetSuite` was `disabled: true` on 2026-08-17
and `disabled: false` on 2026-08-25. Nothing announced it.

**The reconciliation chain is one hop upstream of where Plan A's spec assumed.** Both
`NS - Create Customer Deposits` flows are at **0 errors**. The failures are at *sales
orders never reaching NetSuite*. So the real chain is:

> order fails at the sales-order stage → no NetSuite sales order → no customer deposit →
> unmatched Stripe charge in recon

Plan C must key off the sales-order flows, not the deposit flows. This spec corrects that.

## 2. Goals / Non-goals

**Goals**
- G1. Sync Integrations → Flows → Steps → Scripts into our own tables, sanitized.
- G2. **Snapshot flow errors before Celigo purges them at 30 days.** This table is the
      audit trail; it outlives the source.
- G3. Group errors by signature so "30 errors" reads as "3 root causes".
- G4. Browse the map in Settings — mockup screens 02 (flow map), 03 (flow detail),
      04 (script viewer).
- G5. Detect and record config drift between syncs (enabled/disabled, schedule, mapping).
- G6. Emit a derived flow→NetSuite-record provenance table that Plan C consumes.

**Non-goals**
- N1. **No writes.** No `retry`, no `resolve`. That is PR #204's track.
- N2. No script content into the RAG/knowledge layer or agent context beyond the
      already-shipped live read tools.
- N3. No alerting/notification surface. Drift is *recorded*; routing it is later.
- N4. The recon root-cause card itself — Plan C, which this unblocks.

## 3. Verified platform facts

Everything here was confirmed by live probe, not documentation. Facts already recorded in
`2026-08-17-celigo-connector-design.md` §3 still hold; these are additions or corrections.

| Fact | Evidence |
|---|---|
| Errors carry `purgeAt` ≈ 30 days after `occurredAt` | Observed `occurredAt 2026-08-10` → `purgeAt 2026-09-09` |
| Errors are keyed **per step**, not per flow | The integration-scoped summary reports `steps[]` per flow; a flow may have several failing steps with different counts |
| `list_flow_errors` with `_id` only returns `steps: []` even when errors exist | Reproduced 2026-08-17. Use the `_integrationId` summary, or `_id` + `_stepId` |
| `limit` is ignored in step mode | Asked for 3, received 10 |
| **`retriable: true` is NOT a retry-success signal** | All 30 current errors are marked `retriable: true`; all three classes are deterministic and would fail identically. It means "the platform can re-run it", not "re-running will work" |
| Error objects carry `traceKey` (source-system record id), `errorId`, `retryDataKey`, `source`, `code`, `message`, `_flowJobId`, `occurredAt`, `purgeAt` | Observed across three flows |
| `source` distinguishes failure stage | Seen: `pre_save_page_hook`, `lookup` |
| Flow objects carry `numOpenError` / `lastErrorAt` only when requested | `includeErrorCounts: true` or `hasOpenErrors: true` |
| `aiDescription{summary,detailed}` exists on flows **and** imports/exports | Coverage is uneven; `generatedOn: null` |
| Celigo objects embed captured production payloads | A `mockResponse` contained live `set-cookie` session headers for `.frame.work`, customer records, and inventory |

**UNVERIFIED — must be confirmed during implementation, not assumed:**
`GET /v1/flows/{id}/descendants` is documented to return every referenced step in one call.
I have **not** been able to exercise it (no MCP surface for it), so the client must not be
designed around it without a live check. Fall back to per-id fetches if it does not behave
as documented.

## 4. Architecture

### 4.1 Six tables, all `ENABLE` + **FORCE** ROW LEVEL SECURITY

Per the `092_user_dashboard_preference.py` template, `get_current_tenant_id()` in both
`USING` and `WITH CHECK`. Not the `003` pattern — `connections`/`mcp_connectors` are
RLS-enabled but *not* forced, and the worker role is BYPASSRLS.

| Table | Notes |
|---|---|
| `celigo_integrations` | `celigo_id`, `name`, `sandbox`, `mode`, `description`, `last_modified` |
| `celigo_flows` | `disabled`, `schedule`, `timezone`, `last_executed_at`, `ai_description_*`, `flow_grouping_id`, `source_id` |
| `celigo_flow_steps` | exports + imports unified; `role` ∈ `generator`/`processor`, `adaptor_type`, `connection_celigo_id`, `filter_json`, `mapping_json` |
| `celigo_scripts` | `content`, `content_hash`, `source_id`, `sandbox` |
| `celigo_script_attachments` | `(flow_step_id, script_id, function_name, json_path, site_type)` |
| `celigo_flow_errors` | **the audit trail** — `trace_key`, `error_id`, `retry_data_key`, `source`, `code`, `message`, `occurred_at`, `purge_at`, `flow_job_id`, `signature_id` |

All carry `tenant_id` + `celigo_connection_id`; unique on
`(tenant_id, celigo_connection_id, celigo_id)`.

### 4.2 Error signatures — the thing that makes 30 errors readable

`celigo_error_signatures`: a normalised fingerprint over `(source, code, message)` with
volatile parts stripped — order refs, emails, ids, timestamps. So all four
`MISSING_SHIP_ADDRESS` rows collapse to one signature with `occurrence_count: 4`,
`first_seen`, `last_seen`, and a sample.

**Normalisation must be conservative.** Over-normalising merges genuinely different
failures; under-normalising defeats the point. Start narrow (numbers, emails, `R\d+` order
refs, UUIDs, ISO timestamps) and prove it on the real corpus.

### 4.3 Sanitizer — allowlist, never denylist

Copy only known-safe fields. Everything else is dropped by default, so a new Celigo field
cannot leak. `include`/`exclude` projection keeps the dangerous fields off the wire in the
first place; the sanitizer is defence in depth behind it.

**Error `message` is exempt from stripping and deliberately stored verbatim** — it is the
diagnostic payload. But messages demonstrably contain PII (`deleted_user_242530@user.deleted`,
`mjj@cfnson.com`). Treat the errors table as PII-bearing: no message text in logs, and the
signature fingerprint must strip emails.

### 4.4 Sync worker

`InstrumentedTask`, nightly Beat + manual "Sync now". Inherits the dispatch-even-on-`error`
posture and the freshness-cursor discipline.

Sequencing: integrations → flows → steps → scripts by id → **errors per step** (via the
`_integrationId` summary, then `_id` + `_stepId`; never `_id` alone).

**Errors are append-and-preserve.** Existing rows are NEVER deleted when they vanish from
Celigo — that is the purge we exist to survive. Mark them `resolved_at` / `purged_at`
instead.

### 4.5 Drift detection

Each sync diffs the incoming object against the stored one on a small watched set —
`disabled`, `schedule`, import `mapping_json`, export `filter_json`, script `content_hash` —
and appends to `celigo_config_changes`. Recording only; routing is out of scope (N3).

### 4.6 Provenance (Plan C's input)

Derived from synced import config: `netsuite_da.recordType` + `operation` ⇒ which flows
write which NetSuite record types. Config-derived, not inferred.

**Corrected chain:** the sales-order flows are the ones that fail; the deposit flows are
downstream and currently clean. Plan C traces recon exception → order ref → `trace_key` on
a sales-order flow error.

## 5. UI

Mockup screens 02-04 of the approved artifact
(<https://claude.ai/code/artifact/0c482ad7-6e46-461c-969f-711221e7c69f>), behind the same
default-off `celigo` flag.

Two changes the live data demands:
- The flow map's error column shows **signature count, not raw count** — "3 root causes"
  above "30 errors".
- Script content renders inside the untrusted-content block (arbitrary tenant JavaScript).

Vocabulary is fixed: Source / Destination / Paused / Open errors / Summary.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Cookie/PII ingestion | `exclude` projection + allowlist sanitizer + a test built from the real cookie-bearing payload |
| PII in error messages | Errors table treated as PII-bearing; no message text in logs; signatures strip emails |
| Over-normalised signatures merge distinct failures | Start narrow, validate against the real 30-error corpus, store a sample per signature |
| Losing the purge race | Sync frequency must beat 30 days by a wide margin; alert if the last successful sync exceeds a threshold |
| `/descendants` behaves differently than documented | Verify live before designing around it; per-id fallback |
| Sync cost | 36 integrations × ~19 flows + N+1 scripts, well inside 300 req/s |
| RLS gap | `092` FORCE pattern; explicit BYPASSRLS-role test |

## 7. Decisions (chose X over Y because ___)

- **Snapshot errors** over live-proxy — Celigo destroys them at 30 days; we watched a
  three-week-old defect approach its own deletion.
- **Signature grouping** over raw lists — 30 errors are 3 root causes; the raw list hides that.
- **Deterministic classification** over trusting `retriable` — every current error is marked
  retriable and none would succeed on retry.
- **Allowlist sanitizer** over denylist — payloads demonstrably carry live session cookies.
- **Verbatim error messages** over sanitized ones — the message *is* the diagnosis; the cost
  is treating the table as PII-bearing.
- **Plan B before the write track (#204)** — every current error needs human triage
  (a GDPR data question, a NetSuite dedupe, a script bug); none needs a retry button.
- **Sales-order flows as Plan C's key** over deposit flows — the deposit flows are clean;
  the failure is upstream.

## 8. DON'T

- **DON'T** delete error rows when they disappear from Celigo — that is the purge.
- **DON'T** trust `retriable` as a retry-success signal.
- **DON'T** log error `message` text.
- **DON'T** add these tables without FORCE RLS.
- **DON'T** design the client around `/descendants` until it is verified live.
- **DON'T** feed script content into knowledge profiles or agent context (still N2).
