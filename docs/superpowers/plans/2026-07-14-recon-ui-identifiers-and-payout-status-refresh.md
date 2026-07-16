# Recon UI Identifiers + Payout-Status Refresh — Implementation Doc (handoff)

> **For the next agent:** two independent workstreams, one branch each (or one branch, two commits-per-task, if run as a single session). Both are follow-ups from the live Framework rollout of the summary-first recon surface (PRs #167/#168/#170). TDD throughout; T2 gates apply (recon money path). The dispatch prompt is at the bottom of this doc. This doc lives in gitignored `docs/` — `git add -f` it with your first commit.

## Context (state as of 2026-07-14)

- Summary-first UI is LIVE for the Framework tenant on staging (`recon_resolution_ui` ON; agent flag OFF). Recent runs re-planned post-taxonomy-fix. Settled-week explained rate: 96.6%.
- Group drill-down (`ResolutionGroupItems`) renders narrative + amount + status only — **no identifiers**. Finance can't verify an item against Stripe/NetSuite without leaving the page.
- Fresh-window runs (e.g. 2026-07-07→14) show ~882 `missing_in_netsuite:needs_human` items whose payouts our mirror marks `in_transit` with old `arrival_date`s. Diagnosed 2026-07-14 (staging DB): all 882 have order_reference + payout row + arrival_date present; `payouts.status='in_transit'` for every one. The planner's "still unsettled past the sync-lag window → investigate" rule (PR #170, deliberate) fires on them. Root-cause hypothesis (VERIFY FIRST — task B1): the hourly incremental Stripe sync never revisits old payouts, so `in_transit → paid` transitions are missed and statuses go stale; once refreshed these items should re-plan to `create_and_apply_deposit`.

## Workstream A — identifiers on drill-down items (the user-requested UI change)

**Goal:** every drill-down item shows the identifiers needed to verify it on both sides: order number, Stripe charge id, NetSuite internal id (when a deposit is linked), copyable.

### A1. Backend: enrich `list_group_proposals`

- Files: `backend/app/api/v1/reconciliation.py` (`list_group_proposals`), `backend/app/schemas/reconciliation.py` (`ResolutionProposalResponse`), test `backend/tests/test_resolution_summary_api.py` (extend).
- `ResolutionProposalResponse` gains: `order_reference: str | None = None`, `stripe_charge_id: str | None = None` (alias of the proposal's existing `charge_source_id` — populate from it, don't duplicate storage), `netsuite_internal_id: str | None = None`, `netsuite_record_type: str | None = None`.
- Endpoint: join `ReconciliationResult` (proposal.result_id) for `evidence->>'order_reference'`; LEFT JOIN `NetsuitePosting` on `result.deposit_id` for `netsuite_internal_id` + record type (check the model's column names in `backend/app/models/canonical.py` — the recon skill doc says internal id and record_type exist). Keep it ONE query (join, not N+1); tenant-scope every table.
- Tests: seeded item with matched deposit → all four fields; unmatched (missing) item → order_reference + stripe_charge_id only, netsuite fields None.

### A2. Frontend: render + copy

- Files: `frontend/src/lib/types.ts` (extend `ReconResolutionProposal`), `frontend/src/components/reconciliation/resolution-group-items.tsx`, its test.
- Each item row gains a compact identifier line under the narrative: `R946866359 · ch_3Nxxx · NS#12345` — monospace, muted; each segment a click-to-copy (`navigator.clipboard.writeText`, with a brief "copied" affordance; check for an existing copy-button component in `frontend/src/components/` before writing one). Omit segments that are null.
- The "Investigate in chat" prefill for needs_human items should include the order_reference when present (today it falls back to amount+date; `handleInvestigateProposal` in `page.tsx` — it can now read `p.order_reference` directly).
- Vitest: identifiers render; null netsuite segment omitted; copy handler called with the right string.

### A3. Evidence pack parity (small)

- `_write_proposals_sheet` in `backend/app/services/reconciliation/evidence_service.py` gains Order Ref / Stripe Charge / NetSuite ID columns (the endpoint already dict-ifies proposals — extend the dict + sheet headers + its unit test).

## Workstream B — payout-status refresh (fixes the fresh-window needs_human pile)

### B1. FIRST: verify the staleness hypothesis (read-only)

- Sample ~10 of the 882 `in_transit` payouts (staging DB, Framework tenant) and check against Stripe truth: the tenant's Stripe connection can be exercised via the existing connector service (`backend/app/services/ingestion/` — find the payout fetch used by `stripe_health_check`) OR simply check `payouts.updated_at` vs `arrival_date`: if `updated_at` ≈ row creation and never after arrival, staleness is confirmed structurally. Record findings in your report BEFORE building B2. If the hypothesis is wrong (Stripe really says in_transit), STOP workstream B and report — the planner behavior is then correct and the fix is a Stripe-side operational question.

### B2. Sync: refresh non-terminal payout statuses

- File: `backend/app/services/ingestion/stripe_sync.py` (+ the Celery task that wraps it) — study the incremental sync's cursor/window logic first.
- Add a status-refresh pass to the hourly sync: fetch payouts in NON-TERMINAL states (`pending`, `in_transit`) older than their expected arrival (or simply: all non-terminal rows for the tenant, capped/batched — check volume first; Framework had 16k+ fee rows but non-terminal payouts should be a small set) and update `status` (+ `arrival_date` if changed). Idempotent; batch commits per existing sync conventions; audit/log a summary count.
- Tests: mocked Stripe client — a stale `in_transit` row flips to `paid`; terminal rows not re-fetched; batch/commit cadence per existing sync tests' style.
- Rollout note: after this ships and one sync cycle runs, re-plan recent Framework runs (POST `/reconciliation/runs/{id}/plan-resolutions` ×5 newest) — expect the 882-item group to shrink drastically toward `create_and_apply_deposit`; measure and report the new fresh-window explained rate.

### B3. Optional planner nicety (only if B1 confirms + B2 ships)

- The "still unsettled past window" needs_human narrative could add the payout id for investigation — fold into A1's identifier work if trivial (it's the same `stripe_charge_id`/payout linkage), else skip (YAGNI).

## Constraints (binding, from the program)

- TDD; DB tests: `cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite" DATABASE_URL_DIRECT="" .venv/bin/python -m pytest tests/<file> -v` (usually needs sandbox disabled; NEVER Supabase). Decimal only; tenant-scope every query; no NetSuite writes; engine files off-limits.
- Branch per workstream (`feat/recon-ui-identifiers`, `fix/stripe-payout-status-refresh`); push BOTH remotes; PR per branch; blocking `code-review-multiangle` gate per PR (convergence criterion: stop when a round yields nothing previously unknown; if rounds start relitigating semantics instead of finding bugs, STOP and escalate to the operator).
- FE deploy is manual after merge (buildx with `NEXT_PUBLIC_BUILD_ID=$(git rev-parse --short HEAD)`).
- Framework is a REAL tenant: any live verification uses the R1 method (disposable run deleted by its own UUID; NO approvals, NO flag changes, NO tenant-wide sweeps). uat-smoke is the only tenant where the zero-residue harness may run.

## Dispatch prompt for the next agent (copy verbatim)

```
Read docs/superpowers/plans/2026-07-14-recon-ui-identifiers-and-payout-status-refresh.md in
/Users/aidenyi/projects/ecom-netsuite-suites — it is your full brief (context, two workstreams,
constraints). Execute Workstream A (UI identifiers) first as feat/recon-ui-identifiers, then
Workstream B (payout-status refresh) as fix/stripe-payout-status-refresh — B1's read-only
verification gates whether B2 proceeds. TDD every task; per-PR blocking code-review-multiangle
gate; push both remotes; never push to main directly; Framework live checks use the R1 method
only (doc §Constraints). Report per workstream: commits, test numbers, gate verdicts, and for B
the before/after fresh-window explained rate. Session bookkeeping: create a ClickUp task in
AI-den P0 Active before starting; the prior program trail is ticket 86bawk3cp and memory
project_recon_summary_first_phase1_pr167.
```
