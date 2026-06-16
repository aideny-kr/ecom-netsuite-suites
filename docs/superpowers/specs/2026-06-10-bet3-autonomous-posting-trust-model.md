# Bet 3 — Autonomous Posting Trust Model (DECISION DOC)

> Status: **DRAFT — awaiting operator decision** (ClickUp 86babkn9g says "DESIGN DECISION first, before any code")
> Date: 2026-06-10
> Author: main-thread (research ground-truthed against the codebase at `2deb99f`)

## Why this doc

North-star Bet 3 is "end-to-end scheduled accounting/recon agents that POST / interact with
endpoint systems." The ClickUp task parks it behind an explicit trust-model decision because
autonomous posting deliberately breaks the `no-auto-post / per-line audit` HITL invariant in
CLAUDE.md. This doc frames that decision with codebase ground truth, so the call is made on
facts, not vibes.

## Ground truth (what actually exists today)

Two findings **correct the roadmap's stated baseline**:

1. **"Read + match + schedule DONE" is only ~⅔ true.** Beat schedules `stripe-sync-nightly`
   (01:00 UTC) and `netsuite-deposit-sync-nightly` (02:00 UTC) — both *ingest into our DB*.
   **There is no scheduled reconciliation run**; matching is user-triggered via
   `POST /reconciliation/runs` (`backend/app/workers/celery_app.py` beat_schedule).
2. **Approval never touches NetSuite at any trust level.** Both approve routes
   (`backend/app/api/v1/reconciliation.py:509-693`, `backend/app/mcp/tools/recon_approve.py`)
   are DB status flips + per-line audit. There is **no posting scaffolding at all**: no
   customer-deposit application, no journal-entry creation, no reversal/undo mechanism
   anywhere in the NetSuite service layer. The generic MCP write verbs
   (`ns_createRecord/updateRecord/deleteRecord/upsertRecord`) exist but are HITL-gated
   (HMAC token + WriteConfirmationCard + audit) and are never invoked by recon code.

So Bet 3 is not "remove the human gate from an existing posting flow." It is:
**(a) build the posting capability, then (b) decide how much of it runs without a human.**
These are separable decisions — and that separation is the core of this doc.

Machinery we can reuse (already built and hardened):

| Lever | Where | State |
|---|---|---|
| Four-bucket classification (`matches/rules/auto_classifications/needs_review`) | `four_bucket_classifier.py` | persisted per line |
| Materiality thresholds ($50 abs OR 1% rel, per-tenant) | `materiality.py`, `TenantConfig` | live, drives routing |
| Advisory confidence (amount 0.6 + temporal 0.4) | `confidence_engine.py` | **uncalibrated** (0-approval corpus; R2 slice 2 gated on labels) |
| Per-tenant feature flags + `require_feature` | `feature_flag_service.py`, `dependencies.py` | default-off pattern ready |
| `actor_type="system"` audit + correlation_id batching | `audit.py`, `audit_service.py`, `base_task.py` | already used by jobs |
| Close-lock hard freeze (approve rejected on closed/locked runs) | `reconciliation.py:532-613` | enforced on all 3 approve paths |
| InstrumentedTask Beat conventions (Job record + lifecycle audit) | `base_task.py` | the template for any new scheduled job |

## The autonomy ladder

The binary "HITL vs autonomous" framing hides four distinct rungs. Each rung is independently
shippable and independently gateable:

- **Rung 0 — today.** Human approves matches; nothing ever posts to NetSuite.
- **Rung 1 — autonomous APPROVAL (internal only).** A scheduled job auto-approves lines inside
  a tight envelope (see below). Pure DB status flip, `actor_type="system"`, zero NetSuite
  writes. Breaks "a human approves every line" but **not** "no-auto-post to NetSuite."
- **Rung 2 — HITL POSTING.** Build the actual NetSuite posting feature (deposit application /
  variance journal entry) with a human approving each posting batch — reusing the existing
  mutation_guard + WriteConfirmationCard + HMAC pattern. New capability, old trust model.
- **Rung 3 — autonomous POSTING.** The full bet: a scheduled agent posts to NetSuite inside an
  approval envelope with no human in the loop. Requires Rungs 1+2 plus a reversal story,
  kill switch, and dry-run mode.

Also schedule the missing piece at any rung: a `reconciliation_run` Beat task (read-only
matching on a cadence) — this is uncontroversial, breaks no invariant, and is required
groundwork for "scheduled end-to-end."

## Decision points (operator call)

**D1 — Target rung for Bet 3 v1.**
- (a) Rung 1 only (auto-approve, internal) — lowest risk, no NetSuite writes, immediately demoable as "scheduled agent that closes the easy 95%."
- (b) Rung 2 (build posting, human-gated) — delivers the "POSTs to endpoint systems" headline with the existing trust model intact.
- (c) Rung 3 direct — full autonomy in one program.
- **Recommendation: (a) then (b) as sequential slices, with (c) as a later flag-flip once both have live mileage.** Rationale: Rung 1 generates the human-equivalent approval corpus and operational telemetry that makes the Rung 3 envelope defensible; Rung 2 builds and battle-tests the posting code under HITL before any autonomy touches it.

**D2 — Envelope v1 criteria (what the system may auto-approve / eventually auto-post).**
Proposed, all ANDed:
- bucket == `matches` only (deterministic + zero variance) — exclude `rules` (fuzzy) and `auto_classifications` (has variance) in v1
- match_type == `deterministic`
- variance_amount == 0 (not merely sub-materiality; immaterial-variance lines stay human in v1)
- run not closed/locked (existing hard-freeze guard, reused verbatim)
- per-line dollar cap AND per-run aggregate cap (new `TenantConfig` fields; e.g. line ≤ $10k, run ≤ $250k — operator to set)
- **NOT confidence-gated in v1** — the advisory scorer is uncalibrated (0-approval corpus); using it as a gate would launder an unvalidated number into a financial control. It becomes a gate only after R2 slice-2 calibration.

**D3 — Record-type allowlist for posting (Rung 2+).**
Proposed: `customerDeposit` apply/`depositApplication` only in v1; variance write-off `journalEntry` as v2 (JEs have a native reversal story, but write-offs are a judgment call); everything else stays behind the generic HITL mutation guard. The existing `_BLOCKED_RECORD_TYPES` blocklist remains untouched and supreme.

**D4 — Reversal / rollback story (precondition for Rung 3).**
Nothing exists today. Options: reversal JE generation, record deletion via `ns_deleteRecord`, or NetSuite-native JE auto-reverse. **Recommendation: every autonomous post must record enough in `evidence`/audit to generate a one-click reversal, and the reversal path ships and is e2e-tested BEFORE the first autonomous post.** Non-negotiable for real money.

**D5 — Tenant opt-in + kill switch.**
New flag `autonomous_recon` (default **False** for all tenants, seeded via `DEFAULT_FLAGS`); checked inline by the Beat job per tenant AND enforced via `require_feature` on any admin trigger endpoint. Flag-off must halt the job mid-run safely (idempotent batches keyed by correlation_id). Plus a global env-level kill switch independent of per-tenant flags.

**D6 — Where this sits vs Bet 2.**
Bet 2 (publishable report) is P0 with Slice 1 merged (PR #128) and a due date. This doc deliberately costs only the decision, not the build. **Recommendation: make the D1–D5 calls now, then schedule Rung 1 as the next end-to-end slice AFTER Bet 2's current slice train, unless the operator re-prioritizes.**

## Safeguards that apply at every rung

### Cross-run carry-forward (HARD precondition for enforcement)

Recon results are **per-run snapshots**: nightly scheduled runs over overlapping 7-day
windows re-emit the same underlying line as a fresh non-terminal row in each new run, so a
line a human approved in run N reappears "suggested" in run N+1. Two consequences (flagged
by the T2 gate on PR #129, accepted for the report-only phase):

1. **Dry-run reports over-count repeat candidates across nights.** Each report is honest
   for its own run; aggregating across nights double-counts recurring lines. Acceptable
   for report-only mileage; analysis must treat reports as per-run, not additive. (The
   per-run `already_evaluated` guard prevents re-auditing the *same* run, not the same
   *line* across runs.)
2. **The ENFORCEMENT slice MUST dedupe candidates against lines already dispositioned in
   prior runs** (or recon must carry disposition forward across runs) before any
   system-actor approval. Without this, autonomy would re-approve lines a human already
   reviewed — violating the "already acted on can never be acted on again" invariant at
   the run boundary. This is a blocking design item for Rung 1 enforcement, alongside the
   dollar caps.

- Per-line audit with `actor_type="system"`, `actor_id=NULL`, batch correlation_id — same shape as bulk-approve, so the existing live-smoke assertions extend naturally.
- Dry-run mode first: the job runs in "report-only" for N cycles per tenant, emitting what it *would* have approved/posted; operator compares against human decisions before enabling.
- **Calibration-corpus hygiene:** system approvals must be excluded from (or labeled in) the R2 confidence-calibration corpus — otherwise the model trains on its own output. Persist `approved_by=NULL + actor_type=system` as the discriminator.
- CLAUDE.md invariant update is part of the change: the `no-auto-post` HITL invariant gets rewritten as an *envelope* invariant ("no autonomous post outside the envelope; no autonomous post of record types off the allowlist; reversal path mandatory"), and the change to the invariant itself is T2 by definition (review/UAT policy is a T2 trigger).
- Every slice here is T2: seeded-tenant e2e + safe-envelope live smoke (uat-smoke tenant only) + blocking multi-angle review.

## What happens after the decision

1. Update ClickUp 86babkn9g with the chosen rung + envelope parameters; unpark from P2 if D6 says so.
2. Write the implementation plan (`docs/superpowers/plans/`) for the chosen rung — brainstorm → spec → TDD per house rules.
3. First code slices (likely order): scheduled `reconciliation_run` Beat task (read-only) → dry-run auto-approve job → enforced Rung 1 behind `autonomous_recon`.
