# Recon — Washout Classification + Currency Truth — Phased Plan

> **For agentic workers:** execute via superpowers:subagent-driven-development, one fresh implementer per task, TDD, spec+quality review per task. Phases are separate PRs in order; A gates C.
> Operator decisions (2026-07-21, recorded verbatim): washout = **full refund within 7 days** of the charge + no deposit ever booked; washouts appear as a **visible group with batch Acknowledge**; the sync currency mislabeling is a **standalone P0 fix first**; FX is **mark-only for now** (surface currencies + implied rate; NO fx_variance classification yet, no GL decision).

## Grounded facts (Framework production, 2026-07-21 session; measuring SQL in `.superpowers/sdd/progress.md`)

- `payout_lines`: 44,431 `refund` + 966 `payment_refund` rows, all negative, **100% carry the `R\d{9}` order ref in `description`** — Stripe-side washout join needs no schema change. Shopify `orders`/`refunds` are EMPTY for Framework (0 rows) — do NOT build on them.
- Washouts (charge + same-ref refund netting to |net| < $0.01, last 90d): 2,512 total; 810 same-day; **1,172 within 7 days** (the operator's chosen rule).
- **Sync mislabeling CONFIRMED**: ref-matched deposits labeled CAD/CHF/SGD/NZD have median `deposit.amount / usd_charge.amount` = 1.0000–1.0001 (genuine CAD would be ~1.35) — `netsuite_deposit_sync._DEPOSIT_QUERY` selects `t.total` (subsidiary BASE amount) but labels it `BUILTIN.DF(t.currency)` (TRANSACTION currency). Match-tolerance hit rate: USD-labeled 94.5% vs mislabeled 60–73% — the gap is Stripe-settlement-rate vs NetSuite-booking-rate drift (the amount_mismatch tail).
- Today a washed-out charge → `missing_in_netsuite` → planner rule 7 → **`create_and_apply_deposit` proposal with zero refund signal** (resolution_planner.py rule 7; the wrong-proposal bug this plan kills).

## Global Constraints (all phases)

- Decimal only; tenant-scope every query incl. joins; `amount` on `netsuite_postings` KEEPS its base-currency meaning (tier-1 matching against USD-settled Stripe lines depends on it — do NOT switch it to foreigntotal).
- New root_cause `washout` must NOT enter `RECENCY_HOLD_ROOT_CAUSES` (resolution_planner.py) — a washout is permanent, not "re-check next run".
- Tests: local docker harness ONLY (`cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite" DATABASE_URL_DIRECT="" .venv/bin/python -m pytest …`); NEVER Supabase. Migrations: local docker via `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head` AND `.venv/bin/alembic` → Supabase at deploy (auto).
- Both remotes; never amend; one commit per logical change; squash-merge house style.
- Phases A and B are **T2** (alembic migration / matching-engine + planner change): blocking `Workflow({name:"code-review-multiangle", args:{target:"<PR#>"}})` pre-merge, convergence protocol, plus post-merge disposable-Framework-run acceptance (R1 method: one run, delete by its own UUID, zero approvals/flags/LLM). Phase C is T1.
- SuiteQL dialect: consult `.claude/skills/netsuite-mastery` — `t.foreigntotal` = transaction-currency amount, `t.exchangerate` = base↔txn rate, subsidiary base currency via subsidiary join.

## Phase A — P0: deposit-sync currency truth (branch `fix/netsuite-deposit-currency-truth`, T2)

1. **Migration 090 (additive)**: `netsuite_postings` gains `transaction_currency VARCHAR NULL`, `foreign_amount NUMERIC NULL`, `exchange_rate NUMERIC NULL`. No backfill in-migration (values unknowable offline).
2. **Sync fix** (`backend/app/services/ingestion/netsuite_deposit_sync.py`): `_DEPOSIT_QUERY` additionally selects `t.foreigntotal`, `t.exchangerate`, and the SUBSIDIARY BASE currency (join subsidiary; `BUILTIN.DF(subsidiary.currency)`). Row build: `amount` = `t.total` (unchanged), `currency` = base currency (fixes the lie), `transaction_currency` = `BUILTIN.DF(t.currency)`, `foreign_amount` = `t.foreigntotal`, `exchange_rate` = `t.exchangerate`. Upsert updates the new columns. If the subsidiary join proves unavailable in SuiteQL for this record shape, fall back to leaving `currency` as-is and document — do NOT guess a hardcoded "USD".
3. **Backfill runbook step (post-merge, operator-triggered)**: existing rows self-heal only via re-sync; add a short section to the PR body: run the deposit sync for a trailing 180d window once after deploy (nightly covers 7d). No code needed if the sync entrypoint already accepts a date range — verify and document the exact invocation.
4. Regression: full recon sweep; deposit-sync tests extended (mislabeling pinned RED-first with a foreign-currency fixture). T2 gate → merge → watched deploy → acceptance: re-run the ratio query (in ledger) — newly synced foreign deposits must show honest `transaction_currency` + `exchange_rate`, `currency` = base.

## Phase B — washout classification (branch `feat/recon-washout`, T2)

1. **Refund fetch** (`order_recon_job.py`): ref-keyed fetch of `line_type IN ('refund','payment_refund')` payout_lines for the run's charge refs (mirror the ref-keyed deposit pass; same 90d sanity bound). For each unmatched charge: consider ONLY same-ref refunds dated ≤ 7 days after the charge (`WASHOUT_WINDOW_DAYS = 7`); if those within-window refunds alone net the charge to |net| < $0.01, attach evidence `{washout: true, refund_date, refund_amount, net_after_refund}` to the result. **Operator ruling (2026-07-21, on the partial-then-late-remainder edge): the FULL refund must complete within the window** — slow-trickle refunds mean the order shipped and NetSuite has a booked deposit (reversed via credit memo + refund), so they either match normally or deserve human review; never an auto-acknowledge washout.
2. **Planner rule** (`resolution_planner.py`): new rule inserted BEFORE rule 7 (same precedence pattern as the chargeback gate): evidence.washout ⇒ `root_cause="washout"` (new VarianceType literal; NOT in RECENCY_HOLD_ROOT_CAUSES), `action="carry_forward"`, `booking_vehicle="none"`, narrative "Stripe charge fully refunded on {refund_date} within 7 days; order canceled — no NetSuite booking required." Batch-approvable like other carry_forwards (renders as Acknowledge).
3. **UI labels** (`resolution-groups-table.tsx`): `ROOT_CAUSE_LABEL["washout"] = "Washout — canceled order"`, descriptor "charge refunded, nothing to book"; neutral severity. Exports/groups work automatically.
4. Tests RED-first: charge+full-refund-within-7d → washout proposal (not create_deposit); refund at day 8 → unchanged behavior; partial refund (net > $0.01) → unchanged; washout excluded from recency-hold supersede; e2e seed extended with one washout pair. T2 gate → merge → acceptance: disposable Framework run — expect a washout group in the hundreds on a fresh window; verify a known washout ref classifies correctly; delete run by UUID.

## Phase C — FX mark-only surfacing (branch `feat/recon-fx-marking`, T1, AFTER A)

1. Enrichment (`_enrich_proposal_response` + `_build_proposal_query` + evidence/export columns): add deposit `transaction_currency`, `foreign_amount`, `exchange_rate` from the joined `NetsuitePosting`.
2. Items + needs-human worksheets: when `transaction_currency` differs from the charge currency, NetSuite ID cell gains a muted suffix chip `EUR @ 0.9231` (transaction currency + `exchange_rate`, or implied rate `netsuite_amount/stripe_amount` when exchange_rate is NULL). Exports gain the three columns (xlsx always; CSV per the visible-columns rule).
3. NO classification change — `amount_mismatch` stays `amount_mismatch` (operator: mark-only). Revisit `fx_variance` when accounting picks a GL treatment.

## Out of scope
- fx_variance root cause / FX GL account / looser FX materiality (operator deferred).
- Washouts beyond the 7-day window (operator chose the tighter bound; the residue keeps today's behavior).
- Shopify refunds table (empty for Framework), partial-refund semantics, EUR/GBP/AUD deposits that never ref-match (separate investigation if it matters).
