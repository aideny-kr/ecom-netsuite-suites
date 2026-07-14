# Recon Planner — Order-Level Taxonomy Fix (P1 86bawk3cp) — Implementation Plan

> **For agentic workers:** executed via a build Workflow (sequential implement stages + per-task review + advisory multi-angle phase). Every task self-contained; TDD.

**Goal:** The planner learns the order-level engine's variance vocabulary so real tenants get a real explained rate: `missing_in_netsuite` routes like `missing` (with a recency guard → `carry_forward` for sync-lag), `amount_mismatch` decomposes via fee/rounding evidence, zero-variance fuzzy rows stop generating noise proposals.

**Ground truth (Framework, 2026-07-13):** order engine emits `missing_in_netsuite` (`order_matching_engine.py:50`) and `amount_mismatch` (`order_matching_engine.py:98,101`, `order_fuzzy_matcher.py:110`). Of 21,679 joined `amount_mismatch` rows: 3,073 fee-explained (|abs(variance) − fee| ≤ 0.50), 4,932 ≤ $0.05; residue is sub-materiality relative variance. `payout_lines` has `fee`/`net`/`amount` columns; join key = `evidence->>'charge_payout_line_id'` (uuid); `payouts.arrival_date` gives recency.

**Architecture:** ALL changes planner-side (engine untouched — historical rows keep their strings; the fix is retroactive via idempotent re-plan). `plan_result` gains two optional evidence inputs (`fee_amount`, `days_since_payout`) that `plan_run` supplies via one batched chunk-safe lookup. New/changed rules keep the ordered-first-match contract and the materiality/HITL invariants exactly as they are.

## Global Constraints

- TDD; DB tests: `cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite" DATABASE_URL_DIRECT="" .venv/bin/python -m pytest tests/<file> -v` (sandbox-disable on socket errors; never Supabase).
- Decimal only. Engine output strings NEVER change. Materiality semantics unchanged (still gates bulk-UX + writeoff_je eligibility only). Chargeback gate and human/decided-proposal protections unchanged.
- Thresholds (constants in `resolution_planner.py`, values verbatim): `FEE_EXPLAIN_TOLERANCE = Decimal("0.50")` (mirrors payout classifier), `RECENT_PAYOUT_LAG_DAYS = 7`.
- Branch `fix/recon-planner-order-taxonomy`; one commit per task ending with:
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Lint per house rules; FE `npx vitest run && npx tsc --noEmit` for the FE task.

---

### Task 1: Rule engine learns the order-level taxonomy (pure)

**Files:** Modify `backend/app/services/reconciliation/resolution_planner.py` (`plan_result` + docstring + constants), `backend/app/schemas/reconciliation.py` (`VarianceType` literal gains `"missing_in_netsuite"`, `"amount_mismatch"`); extend `backend/tests/test_resolution_planner.py`.

**Interfaces:** `plan_result` signature gains `fee_amount: Decimal | None = None, days_since_payout: int | None = None` (keyword-only, defaulted — every existing call site keeps working). Rule changes (keep ordered-first-match; renumber docstring):

- NEW rule 2b (after clean-match skip): `match_type == "fuzzy" AND variance_amount == 0 AND variance_type in (None, "")` → **return None** (zero-variance fuzzy = approve-the-match case; classic rules-bucket bulk approve covers it; proposals would be pure noise — this removes the `manual_adjustment … amt=0.00` group observed live).
- Rule 7 (missing) matches `variance_type in ("missing", "missing_in_netsuite")`. NEW sub-branch FIRST: if `days_since_payout is not None and days_since_payout <= RECENT_PAYOUT_LAG_DAYS` → `carry_forward` with narrative "Charge settled recently — NetSuite deposit likely not yet synced; carry forward as a timing item." (`root_cause` stays the RAW variance_type string). Else existing behavior: order_reference known → `create_and_apply_deposit`; unknown → `needs_human`.
- NEW rule 7b (`amount_mismatch`), evaluated in the variance-type dispatch chain:
  1. `fee_amount is not None and fee_amount > 0 and abs(abs_variance - fee_amount) <= FEE_EXPLAIN_TOLERANCE` → `book_fee_line` (narrative: "Variance matches the Stripe processing fee — book as a fee line on the payout's bank deposit."; `root_cause="amount_mismatch"`).
  2. else → reclassify through the EXISTING fx_rounding branch semantics (rule 8): sub-materiality → `writeoff_je`; above materiality → `needs_human`. Implement by delegating to the same code path (extract the rule-8 body into a small local helper if needed — no behavior change for real `fx_rounding` rows).
- `root_cause` on every new path = the raw `variance_type` string (group keys stay honest to source data).

**Tests (exhaustive over new rows; all pure):** missing_in_netsuite+order_ref+old payout → create_and_apply_deposit; missing_in_netsuite+recent payout → carry_forward; missing_in_netsuite+no order_ref → needs_human; amount_mismatch fee-explained → book_fee_line (and NOT above-materiality-blocked — action selection independent of materiality); amount_mismatch small (≤ materiality) → writeoff_je; amount_mismatch large → needs_human; zero-variance fuzzy → None; existing taxonomy rows unchanged (regression: whole existing file green); `days_since_payout=None`/`fee_amount=None` → behave as before (no recency/fee branch). Literal test: `get_args(VarianceType)` includes both new strings.

---

### Task 2: `plan_run` enrichment — batched fee/recency lookup

**Files:** Modify `resolution_planner.py` (`plan_run`); extend `backend/tests/test_resolution_plan_run.py`.

**Interfaces:** after the rows load, collect `charge_payout_line_id`s from evidence (uuid-parse defensively; skip malformed), then ONE batched lookup per 5000-chunk: `SELECT pl.id, pl.fee, p.arrival_date FROM payout_lines pl LEFT JOIN payouts p ON p.id = pl.payout_id WHERE pl.tenant_id=:t AND pl.id IN (...)` → dict. Thread `fee_amount` + `days_since_payout` (computed vs `datetime.now(timezone.utc).date()`; None when arrival_date missing) into `plan_result`. Tenant-scoped; Decimal-safe; missing line id → both None (fully backward-compatible).

**Tests:** seeded result with a payout_line carrying fee → planner emits book_fee_line end-to-end through plan_run; recent arrival_date → carry_forward; absent payout line → falls back to needs_human path (no crash); chunking boundary not required (covered structurally by the existing 5000-chunk pattern).

---

### Task 3: FE labels + summary noise check

**Files:** Modify `frontend/src/components/reconciliation/resolution-group-card.tsx` (`ROOT_CAUSE_LABEL` gains `missing_in_netsuite: "Missing in NetSuite"`, `amount_mismatch: "Amount mismatch"`); extend the card vitest with one label-render case.

---

### Task 4: Live-shaped regression e2e + spec addendum

**Files:** Create `backend/tests/test_resolution_taxonomy_e2e.py`; append addendum to the spec (`docs/superpowers/specs/2026-07-06-recon-summary-first-resolution-design.md`): order-level taxonomy mapping table, recency guard, zero-variance-fuzzy skip, thresholds, and the lesson ("validate against live rows, not spec enums").

**e2e:** seed a run shaped like Framework's live distribution (missing_in_netsuite recent + old, amount_mismatch fee-explained + tiny + large, zero-variance fuzzy, plus one legacy-taxonomy fees row) with matching payout_lines/payouts rows → plan_resolutions → assert: explained_rate > 0 with exact expected group keys/actions; zero-variance fuzzy produced NO proposal; legacy row unaffected. Then full backend suite + FE suite + lint (pre-existing failures triaged as before).

## Post-plan gates (controller)

Build workflow (sequential + advisory multi-angle) → push both remotes → PR → blocking T2 gate (convergence criterion) → merge → staging deploy (+ FE deploy for Task 3) → **acceptance = Framework live e2e re-run** (same safety envelope: disposable run by UUID, no approvals/flags/LLM) measuring the REAL explained_rate → update tickets 86bawk3cp / 86bavjz90.
