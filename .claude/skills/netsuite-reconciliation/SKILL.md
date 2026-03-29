---
name: netsuite-reconciliation
description: >
  NetSuite Reconciliation Engine (v1.3) — Stripe-to-NetSuite payout matching,
  variance classification, evidence packs, and month-end close. Use for reconciliation,
  recon, payout matching, Stripe variance, discrepancy, exception, month-end close,
  or settlement reconciliation.
---

# NetSuite Reconciliation Engine (v1.3 — Shipped)

Deterministic Stripe-to-NetSuite payout matching engine. No LLM in the matching pipeline — all matching uses Decimal math with configurable tolerances.

---

## Architecture: Three-Tier Matching

The `MatchingEngine` runs payouts through three tiers sequentially. Each deposit is consumed at most once (no double-matching).

### Tier 1: Deterministic

Exact payout ID + amount + date matching. Two sub-strategies:

1. **`exact_payout_id`** — `deposit.related_payout_id == payout.source_id`. Confidence 1.0.
2. **`memo_payout_id`** — `payout.source_id in deposit.memo`. Confidence 0.95.

Both require: same currency, amount within rounding tolerance (±$0.05), deposit date within T+0..T+3 of payout arrival.

### Tier 2: Fuzzy

For payouts unmatched after Tier 1. Uses amount-range bucketing (±5%) to avoid O(n^2). Confidence signals are additive, capped at 0.94:

| Signal | Confidence | Condition |
|--------|-----------|-----------|
| `amount_exact` | +0.40 | diff <= $0.05 |
| `amount_within_fx_tolerance` | +0.30 | diff <= 1% |
| `fee_variance` | +0.35 | diff == fee_amount |
| `same_day` | +0.30 | day_diff == 0 |
| `within_N_days` | +0.25 | day_diff <= 3 |
| `memo_contains_payout_id` | +0.20 | payout ID in memo |
| `memo_partial_overlap` | +0.10 | Jaccard word overlap >= 0.5 |

Also attempts:

- **Split-payout matching** — one payout to multiple deposits summing to net_amount. Greedy subset-sum, confidence 0.80, rule `split_payout`.
- **Duplicate detection** — multiple deposits referencing the same payout ID. Match type `exception`, confidence 0.60, variance type `duplicate`.

### Tier 3: Unmatched

Remaining payouts and deposits tagged `match_type="unmatched"`, confidence 0, variance type `missing`.

---

## VarianceClassifier

Classifies the difference between matched payout and deposit into 7 types:

| Type | Condition |
|------|-----------|
| `fees` | Diff matches Stripe `fee_amount` or within $0.50 of it |
| `fx_rounding` | 0 < diff <= $0.05 |
| `timing` | Amount matches but dates differ |
| `missing` | No counterpart on one side |
| `duplicate` | Multiple deposits for one payout |
| `chargeback` | Dispute-related (detected by signals) |
| `manual_adjustment` | Unexplained — requires investigation |

Location: `backend/app/services/reconciliation/variance_classifier.py`

---

## ReconJobRunner

Orchestrates a single reconciliation run: fetch -> match -> classify -> store.

Pipeline steps:
1. Create `ReconciliationRun` record (status `running`)
2. Fetch payouts from `Payout` canonical table (status `paid`, date range, optional subsidiary/payout IDs)
3. Fetch deposits from `NetsuitePosting` (record types: `deposit`, `bankdeposit`, `journalentry`)
4. Run `MatchingEngine.match()`
5. Store `ReconciliationResult` rows with confidence-based auto-status:
   - confidence >= 0.95 -> `auto_matched`
   - confidence >= 0.75 -> `suggested`
   - else -> `pending`
6. Update run with summary counts

Location: `backend/app/services/reconciliation/recon_job.py`

---

## EvidencePackGenerator

Generates 3-sheet Excel workbook (openpyxl):

| Sheet | Contents |
|-------|----------|
| **Summary** | Run ID, period, generated timestamp, auto-matched/suggested/unmatched counts, total variance |
| **All Results** | Full detail table: match type, confidence, status, Stripe/NetSuite amounts, variance, explanation, currency, match rule, payout/deposit IDs |
| **Exceptions** | Filtered to unmatched + confidence < 0.95 |

Row color-coding: green (auto-matched), yellow (exception/suggested), red (unmatched).

Location: `backend/app/services/reconciliation/evidence_service.py`

---

## API Endpoints

All endpoints gated by `require_feature("reconciliation")`. Mutations require `require_permission("recon.run")`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/reconciliation/runs` | List runs (paginated) |
| `GET` | `/reconciliation/runs/{run_id}` | Get single run |
| `POST` | `/reconciliation/runs` | Trigger new run |
| `GET` | `/reconciliation/runs/{run_id}/results` | Get results (filterable by status) |
| `PATCH` | `/reconciliation/results/{result_id}/approve` | Approve a match |
| `GET` | `/reconciliation/evidence/{run_id}` | Download evidence pack Excel |
| `POST` | `/reconciliation/close/{period}` | Close period (YYYY-MM), locks approved/auto_matched results |

Location: `backend/app/api/v1/reconciliation.py`

---

## Schemas

**Request schemas:** `ReconRunCreate` (date_from, date_to, subsidiary_id?, payout_ids?), `ReconResultApprove`, `ReconCloseRequest`

**Internal types:** `PayoutRecord`, `DepositRecord`, `MatchCandidate`

**Response schemas:** `ReconResultResponse`, `ReconRunResponse`, `ReconRunSummary`

**Type literals:**
- `MatchType`: deterministic, fuzzy, unmatched, exception
- `VarianceType`: fees, fx_rounding, timing, missing, duplicate, chargeback, manual_adjustment
- `ResultStatus`: pending, auto_matched, suggested, approved, rejected, investigating, locked
- `RunStatus`: pending, running, completed, failed, closed

Location: `backend/app/schemas/reconciliation.py`

---

## Chat Agent (recon-agent)

YAML config at `backend/app/services/chat/agents/configs/recon_agent.yaml`. Prompt at `backend/app/services/chat/agents/prompts/recon_agent.md`.

**Routing rules** (Tier 1 regex, priority 0):
- `reconcil|recon\b`
- `payout.*match|match.*payout|unmatched.*deposit`
- `exception|variance|discrepancy`
- `stripe.*netsuite.*match|reconcil|compar`
- `month.?end.*close|close.*period|lock.*period`

**Tools:** `recon_run`, `recon_get_exceptions`, `recon_get_evidence`, `recon_approve_match`, `netsuite_suiteql`, `rag_search`

**RAG partitions:** `recon/matching-rules`, `recon/variance-taxonomy`

**Config:** max 10 steps, $0.50 cost budget, `requires_confirmation: true`, `enabled_by_default: false` (feature-flagged).

---

## Key File Locations

| What | Where |
|------|-------|
| Matching engine | `backend/app/services/reconciliation/matching_engine.py` |
| Job runner | `backend/app/services/reconciliation/recon_job.py` |
| Variance classifier | `backend/app/services/reconciliation/variance_classifier.py` |
| Evidence pack | `backend/app/services/reconciliation/evidence_service.py` |
| API endpoints | `backend/app/api/v1/reconciliation.py` |
| Schemas | `backend/app/schemas/reconciliation.py` |
| DB models | `backend/app/models/reconciliation.py` |
| Agent config | `backend/app/services/chat/agents/configs/recon_agent.yaml` |
| Agent prompt | `backend/app/services/chat/agents/prompts/recon_agent.md` |
| Migration | `backend/alembic/versions/062_reconciliation.py` |

---

## Common Pitfalls

1. **All amounts are Decimal** — never use float in matching logic. Rounding tolerance is $0.05, not 5%.
2. **Fuzzy confidence capped at 0.94** — only deterministic matches can reach 0.95+ for auto_match.
3. **Each deposit consumed once** — `consumed` set prevents double-matching across tiers.
4. **Split-payout is greedy** — sorts deposits by amount descending, takes first fitting subset. Not optimal subset-sum.
5. **Date window is directional in Tier 1** — deposit must be T+0..T+3 *after* payout arrival (not before). Tier 2 uses absolute day diff.
6. **Feature flag required** — all endpoints gated by `require_feature("reconciliation")`. Enable per-tenant in `tenant_feature_flags`.
7. **Close period locks results** — status changes to `locked`, run status to `closed`. Irreversible via API.
8. **Variance classifier import is inline** — Tier 2 fuzzy match imports `classify_variance` inside the loop to avoid circular imports.
9. **Evidence pack uses dict conversion** — results are converted to dicts before passing to `EvidencePackGenerator`, not ORM models.
10. **Agent requires confirmation** — `requires_confirmation: true` in YAML. All mutation tools (run, approve) go through confirmation flow.
