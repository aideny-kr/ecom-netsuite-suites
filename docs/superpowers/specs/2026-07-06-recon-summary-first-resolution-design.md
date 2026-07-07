# Reconciliation Rework — Summary-First Review, Agent Resolution, Sub-Ledger Posting

**Date:** 2026-07-06
**Status:** Approved design (brainstormed + section-by-section approval; adversarially reviewed
against codebase, internal consistency, and NetSuite documentation — 18 findings fixed inline)
**Tier:** T2 (mutates customer data, financial posting, HITL invariant, feature flags, migration)

## Problem

The finance team reviews reconciliation mismatches one-by-one: tab-isolated buckets, per-row
approve buttons, top-5 exception cards that each punt to a single-item chat investigation, and
per-bucket bulk approve that never covers `needs_review` — exactly where the pain is. There is
no cross-bucket summary, no grouping by root cause, and no NetSuite posting capability at all
("approve" is a DB status flip; resolution happens manually inside NetSuite).

The team wants:
1. A summary report + grouped list of mismatches instead of item-by-item investigation.
2. Interaction at the summary/group level (batch actions).
3. An agent that tries its best to resolve mismatches before humans see them.
4. Bookings made via **sub-ledger operations** (deposit application, customer refund, credit
   memo, …), not raw journal entries.

## Decisions (locked with operator)

| Decision | Choice |
|---|---|
| Agent autonomy | Agent investigates + proposes; humans **batch-approve groups**; only then does anything post. HITL invariant intact. |
| Primary surface | Redesigned recon page; chat assists with the same tools/actions. |
| Journal entries | Sub-ledger first; aggregate JE allowed only as a **visibly flagged fallback**. |
| Scope | Interactive flow now; proposal/approval/posting rails shared so the scheduled Bet 3 path (trust-model ladder Rung 2/3) plugs in later. This design **is** Rung 2 pulled forward, per `docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md`. |

## Architecture

Three new stages downstream of the existing (unchanged) matching pipeline:

```
OrderReconJob → VarianceClassifier → FourBucketClassifier      (existing, unchanged)
      ↓
ResolutionPlanner        (deterministic, new)
      ↓ unexplained residue only
ResolutionAgent          (LLM, new — proposals only, never postings)
      ↓
ResolutionGroups         (persisted proposals, grouped by root cause × action)
      ↓ human batch-approves a group on the summary page
PostingService           (new) → NetSuite sub-ledger records, audited + idempotent
```

Unchanged: matching engine, four-bucket **classification** (`matches`/`rules`/
`auto_classifications`/`needs_review` — buckets remain the authoritative partition and the
existing per-bucket bulk-approve endpoint keeps its semantics for API compatibility), advisory
confidence (display-only, uncalibrated, never a gate — PR #126 decoupling holds), close-lock
semantics, chat mutation guard.

**Planner input scope:** the ResolutionPlanner consumes results from `rules`,
`auto_classifications`, and `needs_review` (everything except clean `matches`). This means
items in `rules`/`auto_classifications` — today only DB-status-flipped — **will trigger real
NetSuite postings** when their group's action requires one (e.g. explained fee variances →
`book_fee_line`) once `recon_posting` is enabled. That is deliberate: those buckets are where
the mechanically explainable variances live. With `recon_posting` OFF, approving any group is
exactly today's DB flip, just grouped.

### ResolutionPlanner (deterministic, no LLM)

Runs as a post-run step over the input scope above. Maps each result to a proposal using an
ordered rule list (first match wins): **evidence-based rules are evaluated before
`variance_type` dispatch** — e.g. "matched deposit exists but is unapplied" → `apply_deposit`
takes precedence over the variance-type row. Each result matches exactly one rule. Policy gates
baked in:

- **Chargebacks/refund-shaped variances are never auto-proposed as bookings** — proposal is
  `needs_human` with evidence attached (mirrors `_BLOCKED_RECORD_TYPES` philosophy).
- **Timing** variances → `carry_forward`: explicitly *no booking*; annotated as reconciling
  items (never force-matched across periods).
- **Materiality** (see below) gates two things only: (1) one-click bulk-approval eligibility
  in the UI, and (2) `writeoff_je` eligibility. It does **not** otherwise change action
  selection — an above-threshold fee variance is still `book_fee_line`; it just needs an
  individual tick.
- **Cross-run double-posting guard:** planner skips any result whose underlying Stripe charge
  ID / deposit ID already has a `posted` proposal in any prior run; those surface as "already
  resolved in run X". (Closes the carry-forward gap flagged in the Bet 3 decision doc — now
  mandatory since real money posts.)
- Per-item mapping errors abstain to `needs_human`; a bad row cannot kill the plan.

**Materiality source of truth:** the existing R2a config —
`tenant_configs.recon_materiality_abs` / `recon_materiality_pct` (server defaults $50 / 1%),
loaded via `backend/app/services/reconciliation/materiality.py::load_materiality()`. The
effective threshold per item is MIN(abs, pct × order value), same semantics the four-bucket
router already uses. No new config surface; tenants tune the existing columns.

### ResolutionAgent (LLM, tail only)

Picks up planner abstentions (`manual_adjustment`, ambiguous `missing`). Read-only tools
(SuiteQL, Stripe payout lines, existing recon evidence) to investigate; emits either a proposal
with a narrative or `needs_human` with everything gathered. Hard per-item budget + per-run cap;
failures degrade to `needs_human` with partial evidence. **All amounts in narratives come from
tool-computed evidence fields, never model prose** (no-LLM-numbers rule); contract tests enforce
this. The agent writes proposals, never postings.

## Variance → action → NetSuite record mapping

Ordered planner rules (first match wins). "Vehicle" is the canonical `booking_vehicle` used in
`group_key` and the UI chip; secondary records created by multi-write actions are recorded in
`netsuite_record_refs`.

| # | Condition | Action | Vehicle | NetSuite writes | Granularity |
|---|---|---|---|---|---|
| 1 | already `posted` in a prior run (guard) | — skipped, surfaced as "resolved in run X" | — | none | — |
| 2 | matched deposit exists but unapplied (evidence) | `apply_deposit` | `depositapplication` | `depositapplication` (transform from the deposit) | per order |
| 3 | `chargeback` / refund-shaped | `needs_human` (human may select `credit_memo_refund`) | `creditmemo` | `creditmemo` + `customerrefund` | per order, human-initiated only |
| 4 | `fees` | `book_fee_line` | `deposit` | fee line on the payout's Bank Deposit | one per payout (covers all its charge-level fee variances) |
| 5 | `missing`, order ref known | `create_and_apply_deposit` | `customerdeposit` | `customerdeposit` POST → `depositapplication` transform | per order |
| 6 | `duplicate` | `void_duplicate` (with pre-checks, see PostingService) | `customerdeposit` | void/reverse the original `customerdeposit` | per duplicate |
| 7 | `fx_rounding` ≤ materiality | `writeoff_je` | `journalentry` | one aggregate JE per period+currency, entity-tagged | aggregate, **flagged in UI** |
| 8 | `fx_rounding` > materiality | `needs_human` | — | none until human decides | per item |
| 9 | `timing` | `carry_forward` | `none` | none — reconciling-item annotation | n/a |
| 10 | `manual_adjustment` / ambiguous `missing` / anything unmatched by rules 1–9 | → ResolutionAgent → proposal or `needs_human` | per proposal | per proposal | per item |

REST notes (from research + adversarial verification, encoded in payload builders):
- **`depositapplication` is transform-only via REST**: created by
  `POST /record/v1/customerDeposit/{id}/!transform/depositApplication`, never a standalone
  POST to `/depositApplication`. Golden fixtures must assert the transform URL shape, not just
  the body.
- `depositapplication` and `customerrefund` sublists are keyed by the **`doc` field** (target
  transaction internal ID), never positional index — highest-risk integration bug.
- Bank Deposit `exchangeRate` is **believed** create-time-only via REST but this is
  unverified from public docs — default design resolves FX before POST; Phase 3 includes a
  sandbox verification task (PATCH an unposted vs posted deposit's exchangeRate) before the
  no-PATCH assumption is hardcoded.
- A bundled "Chargeback" record type exists on some accounts only — check
  `getRecordTypeMetadata` before ever mapping to it; default is Credit Memo + Customer Refund.
- All target record types (`customerdeposit`, `depositapplication` via transform,
  `customerrefund`, `creditmemo`, `deposit`) are REST-writable; no SOAP fallback needed.

## Data model

**New table `recon_resolution_proposals`** (alembic migration, both DBs; RLS via `tenant_id`):

- `id` UUID PK, `tenant_id`, `run_id` FK, `result_id` FK — one *active* proposal per result;
  re-plans and overrides supersede (`superseded` status).
- `root_cause` — the variance type (real column; drives the summary breakdown; never parsed
  out of a string).
- `action` enum: `book_fee_line` | `create_and_apply_deposit` | `apply_deposit` |
  `credit_memo_refund` | `void_duplicate` | `writeoff_je` | `carry_forward` | `needs_human`.
- `booking_vehicle`: canonical NetSuite record type per the mapping table, `journalentry`, or
  `none`.
- `group_key` — **derived** from (`root_cause`, `action`, `booking_vehicle`), stored for
  indexing convenience (e.g. `fees:book_fee_line:deposit`); queries group by the real columns.
- `source`: `planner` | `agent`.
- `narrative` text; `evidence` JSONB (charge/deposit/payout refs + agent findings).
- `proposed_amount` Numeric + `currency` (Decimal only, tool-computed).
- `status`: `proposed → approved → posting → posted`, or `rejected` / `post_failed` /
  `superseded`.
- `failure_reason` (nullable, set with `post_failed`): `period_locked` | `period_closed` |
  `connection` | `netsuite_validation` | `netsuite_error` | `guard_tripped`.
- `netsuite_record_refs` JSONB — every created record's type+ID stamped after posting
  (traceability + reversal; includes secondary records from multi-write actions).
- `correlation_id`, `decided_by`, `decided_at`, timestamps.

**Groups are computed, not stored** — `GROUP BY (root_cause, action, booking_vehicle)`
aggregation endpoint mirroring the authoritative `/runs/{run_id}/buckets` pattern. Each group:
count, total amount, narrative summary, booking-vehicle badge, materiality split
(under-threshold count = one-click; above = individually ticked).

**Status coupling:**
- Approving a **booking** proposal flips `reconciliation_results.status` to `approved`
  (existing semantics; close/lock logic untouched).
- Acknowledging a **`carry_forward`** group does **not** flip results to `approved`. Results
  get the new status `carried_forward` (added to the `ResultStatus` literal): non-blocking for
  close readiness (counted separately as "N reconciling items carried forward" in the close
  checklist), **not** locked at close, superseded automatically if a later run matches the
  pair. This is the one deliberate close-readiness change in this design.
- Rejection returns the result to `needs_review` with agent evidence retained.

## API

Following existing bulk-approve conventions (set-based SQL, per-line audit rows + one summary
event, `correlation_id`; skips anything no longer `proposed`, returns honest approved/skipped
counts):

- `GET  /reconciliation/runs/{run_id}/resolution-summary` — match rate, explained rate,
  variance by root cause, group list.
- `POST /reconciliation/runs/{run_id}/resolution-groups/{group_key}/approve`
  (body: audit note, optional per-item exclusions — excluded items simply stay `proposed`)
  and `/reject`.
- `PATCH /reconciliation/resolution-proposals/{id}` — override action or send to human.
  Semantics: the original proposal is marked `superseded` and a new active proposal is created
  for the same `result_id` (preserves the one-active-proposal invariant and the audit chain).
- Posting progress via SSE (same pattern as run pipeline) + retry endpoint for `post_failed`.
- Existing endpoints (`/buckets`, `/approve-bucket`, single approve, evidence, close) keep
  their current semantics — regression-locked.

**Permissions & flags:**
- New `recon.post` permission distinct from `recon.run` (segregation of duties: reviewers
  approve, posting-permitted users post).
- New feature flag `recon_posting` (default OFF, per-tenant) gates PostingService.
- New feature flag `recon_resolution_ui` (default OFF, per-tenant) gates the redesigned page
  surface, so the IA change stages and rolls back independently of the money path. Flag off =
  today's tab UI, untouched.
- All endpoints remain behind `require_feature("reconciliation")`.

## PostingService

- **Trigger boundary:** fires only from an approved group — page approval *is* the HITL
  confirmation. Celery task on its own queue; SSE progress to the page. Nothing posts
  synchronously; nothing posts from chat directly.
- **Chat path:** `recon.approve_group` chat tool presents a confirmation card and reuses only
  the **low-level one-use HMAC helpers** (`generate_confirmation_token` /
  `verify_confirmation_token`) from `write_confirmation_service`. It does **not** reuse
  `build_confirmation_payload` (coupled to the external-MCP create/update/delete/upsert +
  record-type contract) and is not auto-detected by `classify_mutation()` (which matches
  `ext__<hex>__ns_*` tool names only) — recon gets its own small confirmation payload builder,
  converging on the same approve endpoint as the page.
- **Compile step:** approved proposals → booking instructions with the granularity in the
  mapping table (fees aggregate per payout; JE write-offs aggregate per period+currency; the
  rest per order/record).
- **Idempotency:** every created record carries NetSuite `externalId` =
  `correlation_id + instruction key`; retries upsert, never duplicate.
- **Period safety — three branches, checked per instruction:**
  - **OPEN** → post normally.
  - **LOCKED** → `post_failed: period_locked`; UI offers "post into current open period (memo
    references original date)" or, only if the connection's role holds Override Period
    Restrictions, an explicit human-confirmed override. Never silent.
  - **CLOSED** → `post_failed: period_closed`; override permission does **not** work on closed
    periods (reopening is the only path) — no override attempt is ever made; UI offers only
    "post into current open period" or manual handling.
- **`void_duplicate` pre-checks (policy gate, mirrors chargeback gate):** before voiding,
  query whether the target `customerdeposit` has an existing `depositapplication` against it
  or participates in a bank-reconciliation match — if either, abstain to `needs_human` (an
  applied deposit must be unapplied or refunded, not voided). Phase 3 sandbox task: confirm
  the account's "Void Transactions Using Reversing Journals" preference, which changes whether
  void or delete is the correct REST operation.
- **Failure isolation:** per-instruction try/catch; one failure never aborts the batch;
  failures carry `failure_reason` + the NetSuite error in `evidence` and are retryable from
  the UI. NetSuite connection failure (known single-use refresh-token death) →
  `post_failed: connection`, stop the batch, surface loudly — no retry-looping a dead
  connection.
- **Reversal (v1):** posted record refs stored per proposal; the void writer built for
  `void_duplicate` doubles as the manual "reverse this posting" action. Automated reversal
  orchestration remains a Rung 3 concern.

## UI (recon page rework — behind `recon_resolution_ui`)

- **Summary header** (replaces static 4-card bar): match rate, total variance, **explained
  rate** (% of exceptions resolved into proposals — diagnostic, not vanity: a falling rate
  signals upstream data problems), variance-by-root-cause breakdown (queries the `root_cause`
  column).
- **Resolution group cards** replace bucket tabs as the primary surface: root-cause label,
  plain-language narrative, count, total, booking-vehicle chip (**JE fallback chip renders
  amber/flagged**), materiality split. Actions: Approve group (with audit note), Reject,
  Review items.
- **Drill-down demoted, not removed:** expanding a card shows the item table (evolved
  `ReconResultsTable`: checkboxes for exclusions, advisory score sortable/filterable). An
  "All results" view with bucket filter pills replaces the tabs for audit purposes.
  "Investigate in chat" lives inside the **Needs human** group, seeded with the agent's
  narrative + evidence.
- **Timing group**: action is "acknowledge as carry-forward" — results → `carried_forward`,
  no posting.
- **Posting feedback:** approved card → posting-progress state → per-line results (NetSuite
  record links on success; inline `failure_reason`-specific error + retry on failure —
  `period_closed` gets the explicit-choice prompt, `connection` gets the re-auth banner).
- **Close checklist** becomes answerable: "what's blocking close" links to the specific
  unresolved groups; carried-forward items shown as a separate non-blocking line.
- **Agent progress:** "agent investigating N items…" with groups filling in progressively.
- Kept: `DataFreshnessBanner`, `ReconProgressStepper`, run picker, evidence pack (extended
  with a Proposals sheet).
- **Chat parity:** new tools `recon.get_resolution_summary` and `recon.approve_group`
  (confirmation card per PostingService section). Existing single-item tools remain for tail
  investigation.

## Error handling summary

Planner abstains per-item; agent degrades to `needs_human` on budget/timeout/failure; group
approval skips non-`proposed` rows (concurrency-safe, honest counts); posting isolates
per-instruction failures, is idempotent on retry, refuses locked/closed periods without an
explicit human choice (and never attempts overrides on closed periods); void pre-checks
abstain on applied/reconciled deposits; close-lock rules untouched except the deliberate,
explicit `carried_forward` addition.

## Testing (TDD; T2 gates)

- **Unit:** planner rule table exhaustively (all 10 ordered rules incl. precedence
  — evidence-based before variance-type — chargeback and void pre-check policy gates,
  materiality boundary for `writeoff_je` vs `needs_human`, cross-run double-posting guard);
  group-key derivation from columns; **golden payload fixtures** per sub-ledger writer —
  asserting the `depositapplication` **transform URL shape** and `doc`-keyed sublists.
- **API:** resolution-summary aggregation; approve/reject/exclusion/override-supersede
  semantics + audit rows + correlation IDs; `recon.post` vs `recon.run`; both new flags off →
  gated.
- **Posting:** mocked NetSuite client — failure isolation, retry idempotency (same
  `externalId` → upsert), all three period branches (open/locked/closed), void pre-check
  abstention.
- **Close:** `carried_forward` non-blocking semantics; everything else regression-locked.
- **Chat:** tool registration → `backend/tests/test_prompt_tool_sync.py` capability-sync
  invariant; confirmation-card flow for `recon.approve_group`; agent narrative contract test
  (amounts only from evidence fields).
- **E2E:** seeded-tenant CI e2e extended: run → plan → approve fee group → assert posting
  instructions + audit trail. Live smoke inside the uat-smoke safe envelope; posting smoke
  points **only at a sandbox NetSuite account**, never production.
- **Frontend:** vitest (group cards, materiality split, amber JE chip); Playwright
  (approve-to-posted flow).
- **Regression:** full existing recon suite green — close, per-bucket bulk approve, evidence
  pack semantics unchanged.
- **Pre-merge:** blocking multi-angle review (`code-review-multiangle`) per phase, per T2
  policy.

## Phasing & rollout

Four phases, each an independently-reviewable PR with its own T2 gate:

1. **Phase 1 — groups without money:** migration + ResolutionPlanner + resolution-summary /
   group-approve endpoints + page rework behind `recon_resolution_ui` (OFF). Posting disabled;
   group approve = today's DB-flip semantics, grouped. Includes `carried_forward` status +
   close-readiness change.
2. **Phase 2 — agent tail:** ResolutionAgent + narrative contract tests + chat tools.
3. **Phase 3 — posting:** PostingService + payload builders + `recon_posting` flag +
   `recon.post` permission + posting UI feedback + sandbox verification tasks (exchangeRate
   PATCH behavior; void-vs-reversing-journals preference).
4. **Phase 4 — enablement:** uat-smoke live smoke (sandbox NetSuite), then enable for
   Framework after **3 consecutive clean cycles** — defined as: zero `post_failed` other than
   explicit `period_locked`/`period_closed` human-choice outcomes, zero `guard_tripped`
   (double-posting guard), zero unexplained `needs_human` spikes vs baseline. Operator (Aiden)
   flips the flag; the scheduled Bet 3 path adopts the same rails (envelope → auto-approve
   within envelope → PostingService) as a later, separate slice.

## Out of scope (v1)

- Automated reversal orchestration (Rung 3).
- Confidence-gated auto-approval (advisory composite stays display-only until calibrated —
  R2 Slice 2).
- Vendor/AP-side reconciliation.
- Learned-rule promotion from resolution decisions (worth a follow-up: recon rule tuning is a
  legitimate, narrower re-entry point for the disabled auto-learning pattern).

## Key existing files touched / referenced

| What | Where |
|---|---|
| API routes | `backend/app/api/v1/reconciliation.py` |
| Four-bucket classifier | `backend/app/services/reconciliation/four_bucket_classifier.py` |
| Variance classifier | `backend/app/services/reconciliation/variance_classifier.py` |
| Materiality loader (R2a, reused as-is) | `backend/app/services/reconciliation/materiality.py`, `tenant_configs.recon_materiality_abs/pct` |
| Advisory confidence | `backend/app/services/reconciliation/confidence_engine.py` |
| Autonomy envelope (Bet 3 Rung 1) | `backend/app/services/reconciliation/autonomy_envelope.py`, `backend/app/workers/tasks/recon_envelope_dry_run.py` |
| Chat recon tools | `backend/app/mcp/tools/recon_*.py` |
| HITL token helpers (only the HMAC primitives reused) | `backend/app/services/chat/write_confirmation_service.py`, `mutation_guard.py` |
| Recon page | `frontend/src/app/(dashboard)/reconciliation/page.tsx` + `frontend/src/components/reconciliation/*` |
| Trust-model ladder | `docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md` |
