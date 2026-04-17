# Design: NetSuite Knowledge Profile + RAG Ingest + Admin Seed Pipeline (Phase 2)

Generated 2026-04-16 — follows PR #45 (Phase 1 hotfix). Consolidates and refines the Phase 2 section of `docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md`.
Status: DRAFT
Mode: Intrapreneurship

## Problem Statement

Phase 1 stopped the bleeding on the shipping-country regression by adding the ADDRESS TABLES rule to the universal prompt and replacing the `_need_patterns` gate. But the structural fragility that allowed the regression remains:

1. **No `netsuite.yaml` knowledge profile** — SuiteQL dialect rules sit in `unified_agent.py`'s `_SYSTEM_PROMPT` and ship to every tenant, even those with no NetSuite tools connected. Plus the `_SYSTEM_PROMPT` ceiling had to be bumped from 18000 → 18500 chars to fit the ADDRESS TABLES block; it'll keep growing.
2. **Golden dataset orphaned** — 14 `knowledge/golden_dataset/*.md` files (suiteql syntax, joins, transactions, multi-currency, record types) exist as engineer reference but are NOT loaded into RAG. The agent never sees them at chat time.
3. **No admin-seed pipeline** — Framework's 6 shipping-country patterns were auto-learned in April. Auto-learning is now disabled. There's no script to seed new tenants or restore lost patterns; the only path is a one-off SQL insert.
4. **No worked example for the canonical `transactionShippingAddress` pattern in golden dataset** — even after Phase 2 ingests the existing files, the canonical "join + revenue filters" example still has to be written.

Phase 2 fixes all four. Net result: every NetSuite tenant gets the SuiteQL dialect rules via `netsuite.yaml` only when NS tools are connected; `golden_dataset/` becomes a live RAG corpus instead of dead docs; admin-seeded patterns become a scriptable operation; and the canonical shipping-country query becomes a worked example others learn from.

## Demand Evidence

- **PR #45 success criterion mentions Phase 2 explicitly:** "Three independent staging tests: 'Show sales by shipping country since Jan 1' → first-attempt correct SQL. 'Compare net revenue Q1 vs Q2 by subsidiary' → uses correct multi-currency rules from RAG. 'Pivot order counts by month and country' → uses pivot tool, correct join. All three should pass without the agent rediscovering joins or status codes." Today only the first works (because Phase 1 added one block to the prompt). The other two need RAG-injected golden dataset content.
- **Base prompt budget pressure:** PR #45 bumped `_SYSTEM_PROMPT` ceiling from 18000 → 18500 to fit one new block. Adding more universal rules without moving the existing ones to `netsuite.yaml` will keep cracking the ceiling.
- **Pattern restoration cost:** the next time a tenant's patterns get lost or corrupted (e.g., from a future schema migration), we'd repeat the 4-hour SQL-script + manual-DB dance from this morning. An admin-seed CLI eliminates that toil for every future incident.
- **CLAUDE.md known issue #2** still reads: "auto-learning from live sessions DISABLED. Only admin-seeded or nightly-promoted patterns are retrievable. 6 verified shipping-country patterns + 1 RAG chunk seeded for Framework." The "+ 1 RAG chunk" claim is fictional — there are no RAG chunks for NetSuite. Phase 2 makes that claim true.

## Status Quo

- `netsuite.yaml` does not exist. Existing profiles: `bigquery.yaml`, `pricing.yaml`, `reconciliation.yaml`, `netsuite_writes.yaml`, `cross_source.yaml`, `google_sheets.yaml`.
- `unified_agent.py:140-350+` holds ~150 lines of SuiteQL dialect rules in the universal `_SYSTEM_PROMPT`, plus the new ADDRESS TABLES block from Phase 1.
- `knowledge/golden_dataset/` has 14 `.md` files. None of them are referenced by any profile's `rag_partitions`. They've never been ingested into `domain_knowledge_chunks`.
- `backend/scripts/` has `ingest_domain_knowledge.py` (used for other RAG content) but no `seed_tenant_patterns.py`.
- `_INVESTIGATION_RE` in `orchestrator.py` classifies queries as `FULL` based on heuristics like "why", "how come", "audit". Phase 1 fixed pattern gating for FULL; this spec audits whether vernacular/domain-knowledge gating still has gaps for these queries.

## Target User & Narrowest Wedge

**Target:** any NetSuite tenant (not just Framework) who asks queries that require SuiteQL dialect knowledge or patterns from `golden_dataset`.

**Narrowest wedge:** the three Phase 1 staging tests pass on a fresh chat session for Framework, AND repeating the test on a hypothetical second NS tenant (no admin-seeded patterns) shows the netsuite.yaml profile injecting the SuiteQL rules. Cost on FULL queries does not regress beyond the Phase 1 baseline.

## Constraints

- **No DB migrations** for the YAML profile + RAG ingest paths (existing `domain_knowledge_chunks` table sufficient). `seed_tenant_patterns.py` uses existing `tenant_query_patterns` table (migration 034).
- **No regression on Phase 1 success criterion.** The shipping-country canonical case must still pass first-attempt with `sa.nKey = t.shippingAddress`.
- **Verbatim copy of SuiteQL rules** when moving from `_SYSTEM_PROMPT` into `netsuite.yaml.prompt_fragment` (CLAUDE.md rule #24). No paraphrasing.
- **No re-enable of auto-learning.** Patterns enter the system only via admin-seed (this spec) or eval-gated nightly promotion (the autonomous-improvement skill, separate spec).
- **Knowledge profile loader pattern unchanged.** `netsuite.yaml` looks structurally identical to `bigquery.yaml` and follows the existing `KnowledgeProfile` schema in `loader.py`.

## Premises

1. **Universal SuiteQL rules belong in `netsuite.yaml`, not `unified_agent.py`.** Moving them shrinks the base prompt for non-NS tenants, removes the duplication risk CLAUDE.md rule #24 warns about, and lets the ceiling test ratchet back down to ~13000 chars.
2. **The golden dataset is a wasted asset until it's ingested.** Engineers wrote 14 files of NetSuite domain knowledge and the agent has never seen any of them at chat time. Ingestion is mostly mechanical — the existing `ingest_domain_knowledge.py` script handles the work.
3. **Pattern seeding will be needed again.** Every time we onboard a new tenant, recover from a corruption, or run an autonomous-improvement promotion, we need the same operation. A script is cheaper than a runbook.
4. **The worked `transactionShippingAddress` example deserves a permanent home in the golden dataset.** The Phase 1 fix put the join in the unified prompt as a rule, but a complete worked example (with mainline/taxline/iscogs/assemblycomponent filters and a worked SUM(tl.amount * -1)) belongs in `join-patterns-and-aggregation.md` for RAG retrieval. The two channels reinforce each other.
5. **The `_INVESTIGATION_RE` audit is small but high-leverage.** Phase 1 fixed `_need_patterns`. The same FULL classification still skips `_need_vernacular` and `_need_domain_knowledge`. If a tenant asks "why are these countries' sales lower than last quarter," the FULL path should still inject vernacular + DK or risk repeating the regression in those slices.

## Approaches Considered

### Approach A: Profile + RAG ingest only (rejected as standalone)
Write `netsuite.yaml` and ingest the 14 golden_dataset files. Skip the seed script and the `_INVESTIGATION_RE` audit.
- **Effort:** S (~3-4 hours).
- **Pros:** Smallest scope, ships fastest.
- **Cons:** Doesn't address pattern seeding (next regression repeats the manual SQL). Doesn't audit the gating fix's coverage.

### Approach B: All five sub-tasks in one PR (rejected as standalone)
Profile + RAG ingest + seed script + worked example + investigation audit.
- **Effort:** M (~1-2 days).
- **Pros:** Complete. Mirrors the original Phase 2 plan.
- **Cons:** Larger blast radius, harder to roll back any one piece.

### Approach C: Two-PR split (CHOSEN)
- **PR A** (this spec): `netsuite.yaml` profile + golden dataset RAG ingest + worked example. The user-visible improvement: more NetSuite questions answer correctly because RAG has more to draw on.
- **PR B** (separate): `seed_tenant_patterns.py` admin script + `_INVESTIGATION_RE` audit. The internal-tooling improvement: faster recovery from future regressions, narrower context-need leaks.

- **Effort:** PR A ~6 hours, PR B ~3-4 hours.
- **Pros:** Each PR has a single clear theme, clean rollback boundary, independent reviewability.
- **Cons:** Two PRs instead of one. PR B may slip if priorities shift.

## Recommended Approach

**Approach C, PR A** is the scope of this spec. PR B gets its own spec when prioritized.

### PR A — `netsuite.yaml` + RAG ingest + worked example

**1. Create `backend/app/services/chat/knowledge_profiles/netsuite.yaml`.**

Schema mirrors `bigquery.yaml` exactly. `trigger_tools` covers ALL NetSuite read-side tool names (per the plan in PR #45, validated against `tool_categories.py`):

```yaml
profile_id: netsuite
display_name: "NetSuite SuiteQL"
trigger_tools:
  - netsuite_suiteql
  - netsuite_financial_report
  - ns_runReport
  - ns_runSavedSearch
  - ns_listSavedSearches
  - ns_listAllReports
  - ext__*__ns_runCustomSuiteQL
  - ext__*__ns_getSuiteQLMetadata
  - ext__*__ns_getRecord
  - ext__*__ns_runReport
  - ext__*__ns_runSavedSearch
  - ext__*__ns_listSavedSearches
  - ext__*__ns_listAllReports
  - ext__*__ns_getSavedSearchSchema
prompt_fragment: |
  ## NetSuite SuiteQL Context

  [VERBATIM COPY of unified_agent.py SuiteQL dialect rules (lines ~173-313),
   INCLUDING the ADDRESS TABLES block from Phase 1.]
rag_partitions:
  - netsuite/suiteql-rules
  - netsuite/joins
  - netsuite/transactions
  - netsuite/multi-currency
  - netsuite/record-types
```

**2. Move SuiteQL dialect rules out of `unified_agent.py` `_SYSTEM_PROMPT`.**

Cut lines ~173-313 (the SuiteQL dialect block, including ADDRESS TABLES) from `_SYSTEM_PROMPT` and paste them verbatim into `netsuite.yaml`'s `prompt_fragment` block (under the `## NetSuite SuiteQL Context` header). The unified prompt now only ships these rules when an NS tool is connected.

**Risk mitigation:** add a regression test asserting `netsuite.yaml.prompt_fragment` contains the same key rule strings (`"ADDRESS TABLES"`, `"sa.nKey = t.shippingAddress"`, `"single-letter status"`, `"FETCH FIRST"`, `"BUILTIN.DF"`) as the pre-move `_SYSTEM_PROMPT`. The test prevents accidental rule loss in the move.

**Tighten the prompt-size ceiling test** in `backend/tests/test_prompt_trim.py` from 18500 back down to 13000 (matches the docstring's "ratchet down" plan). The non-NS portion of the prompt is ~12K chars after the move.

**3. Ingest the 11 NetSuite-specific `golden_dataset/*.md` files via `ingest_domain_knowledge.py`.**

Mapping (per the table in PR #45's spec):

| Partition | Files |
|---|---|
| `netsuite/suiteql-rules` | `suiteql-syntax-rules.md`, `suiteql-example-queries.md`, `common-errors-and-recovery.md`, `date-and-time-patterns.md` |
| `netsuite/joins` | `join-patterns-and-aggregation.md`, `transaction-relationships.md` |
| `netsuite/transactions` | `transaction-types-and-statuses.md`, `financial-statements.md` |
| `netsuite/multi-currency` | `multi-currency-rules.md` |
| `netsuite/record-types` | `record-types-and-columns.md`, `custom-fields-and-records.md` |

Each ingest call: `python -m backend.scripts.ingest_domain_knowledge --file knowledge/golden_dataset/<file>.md --partition <partition_id>` (or whatever the actual CLI signature requires — verify before scripting).

The 3 BigQuery files (`bigquery-bi-analytics.md`, `bigquery-data-transformation.md`, `bigquery-sql-patterns.md`) are out of scope for this PR; verify they're already in `bi/*` partitions referenced by `bigquery.yaml` and file a follow-up if not.

Document the ingest commands in `docs/runbooks/netsuite-rag-ingest.md` so future re-ingest is a script step, not tribal knowledge.

**4. Append a worked `transactionShippingAddress` example to `knowledge/golden_dataset/join-patterns-and-aggregation.md`.**

Add a new section at the END of the file (preserves existing chunk indices per CLAUDE.md):

```markdown
## Worked Example: Sales by Shipping Country

Pattern verified 2026-04-09 (Olivia's session) and re-validated 2026-04-16 (Phase 1 staging benchmark). Burned 10+ tool calls when the agent had to rediscover the join.

```sql
SELECT
  BUILTIN.DF(sa.country) AS ship_country,
  COUNT(DISTINCT t.id) AS total_orders,
  SUM(ABS(tl.quantity)) AS total_qty,
  ROUND(SUM(tl.amount * -1), 2) AS revenue_usd
FROM transaction t
JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'SalesOrd'
  AND t.status NOT IN ('C', 'H')
  AND sa.country IN ('NO', 'CH', 'NZ', 'SG')
  AND tl.mainline = 'F'
  AND tl.taxline = 'F'
  AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
  AND tl.assemblycomponent = 'F'
GROUP BY BUILTIN.DF(sa.country)
ORDER BY revenue_usd DESC
FETCH FIRST 100 ROWS ONLY
\```

Critical details:
- Join key is `sa.nKey = t.shippingAddress` (NOT `sa.recordOwner = t.id`, NOT `sa.transaction = t.id`).
- Use `BUILTIN.DF(sa.country)` for display name, `sa.country` for 2-letter ISO.
- Do NOT use `custbody*_country*` custom fields; the standard address join is the source of truth.
- Single-letter status codes (`'C'`, `'H'`) — never compound (`'SalesOrd:C'`).
```

Re-ingest just this file after appending. The append-only edit keeps prior chunks' positions stable.

**5. Verify retrieval works for the three Phase 1 success-criterion queries.**

Run a diagnostic script (not committed) that:
1. Embeds each test query.
2. Calls `retrieve_domain_knowledge()` with `partition_ids=netsuite/*`.
3. Prints the top-3 chunks and similarity scores.

Expected: each query retrieves at least one relevant chunk with similarity ≥ 0.50 (the default DK threshold). If similarity is below threshold, ingestion may need adjustment (chunk size, embedding model parameters).

### PR A deliverable

One PR against main with:
- `backend/app/services/chat/knowledge_profiles/netsuite.yaml` (new).
- `backend/app/services/chat/agents/unified_agent.py` (SuiteQL block removed).
- `backend/tests/test_prompt_trim.py` (ceiling tightened 18500 → 13000).
- `backend/tests/test_unified_agent_suiteql_rules.py` (assertions point at `netsuite.yaml` instead of `_SYSTEM_PROMPT` for the moved rules).
- `backend/tests/test_netsuite_yaml_profile.py` (new — verifies profile loads, trigger_tools match expected, prompt_fragment contains all key rules).
- `knowledge/golden_dataset/join-patterns-and-aggregation.md` (worked example appended).
- `docs/runbooks/netsuite-rag-ingest.md` (new — documents the ingest commands).
- DB write: 11 files ingested into `domain_knowledge_chunks` (runbook step, not part of the PR diff).

## Open Questions

1. **Existing benchmark cases break?** Once SuiteQL rules move out of `_SYSTEM_PROMPT`, agents for tenants that have only BigQuery + Pricing tools will no longer see the SuiteQL rules. Verify (via Framework tenant on staging) that no benchmark case relies on a SuiteQL rule injection it no longer gets. Likely safe — Framework HAS NetSuite tools — but worth a single benchmark run with `netsuite.yaml` disabled to confirm the regression risk.
2. **Profile loader handles fnmatch globs correctly?** `trigger_tools` includes `ext__*__ns_runCustomSuiteQL` glob patterns. Verify `loader.py::matches_tools()` handles these via fnmatch (not exact match). The existing `bigquery.yaml` uses exact tool names; this is the first profile to lean on globs heavily for NetSuite.
3. **Should the ingest run as part of CI/CD or as a manual runbook?** Manual is simpler for a one-time operation. But future updates to golden_dataset files would require a remembered manual step. Consider a Celery task that watches the directory.
4. **What about `golden_dataset` files added later?** New files won't auto-ingest. Either: (a) document the convention (runbook), (b) add a CI check that fails when a new file lands without a corresponding ingest commit, (c) auto-ingest via Celery task triggered on PR merge to main.
5. **Cross-source queries with NS + BQ both connected** — both `netsuite.yaml` and `cross_source.yaml` will inject. That's by design; the disambiguation prompt mediates. Verify the combined prompt is still under the (newly tightened) ceiling.

## Success Criteria

- All three Phase 1 staging-test queries pass first-attempt on a fresh chat session as Framework:
  - "Show sales by shipping country since Jan 1" — uses `transactionShippingAddress` join from RAG-retrieved worked example.
  - "Compare net revenue Q1 vs Q2 by subsidiary" — uses multi-currency rules from RAG.
  - "Pivot order counts by month and country" — uses pivot tool, correct join.
- `vs-MCP` benchmark holds at ≥ 16 wins out of 18 (no regression).
- Base prompt size drops from 18351 → ~12500 chars (after SuiteQL block move). Ceiling test passes at 13000.
- All 11 NetSuite golden dataset files retrieve ≥ 0.50 similarity for at least one Phase 1 staging-test query.
- New `netsuite.yaml` profile triggers ONLY when at least one NS tool is in the toolset (verified via integration test against a tenant with only BigQuery tools).

## Distribution Plan

Existing CI/CD pipeline. After PR #46 (deploy.yml fix) lands, deploys can be triggered via workflow_dispatch reliably for any branch. The RAG ingest runs as a one-off `.venv/bin/python` against staging Supabase before merging the PR, so the PR's first staging deploy already has the chunks loaded.

## Dependencies

- PR #45 merged (Phase 1 hotfix — provides the ADDRESS TABLES block this PR moves into the profile).
- PR #46 merged or in flight (deploy.yml SHA-tag fix — makes feature-branch deploys reliable).
- `ingest_domain_knowledge.py` script (existing).
- `KnowledgeProfile` loader (existing).
- `domain_knowledge_chunks` table (existing — migration 047 or thereabouts).
- No new external dependencies. No new env vars. No new packages.

## The Assignment

Before starting implementation: pull the current `_SYSTEM_PROMPT` content, count its lines and chars, and identify the exact boundary where the SuiteQL block starts and ends. The "verbatim copy" requirement only works if the boundary is unambiguous. If a rule is half-inside / half-outside the SuiteQL block (e.g., the BUILTIN.DF rule at line 195 is conceptually part of "field resolution" but lives in the general TEXT RESOLUTION section), make a deliberate decision about which side it goes on and document it in the PR description. Three minutes of reading saves an hour of "wait, did this rule get lost?" debate during review.
