# grill-me — SuiteQL ship-to-country perf guard (PR #118)  (diff mode)
> 2026-06-05 · Target: `fix/suiteql-address-perf-guard` vs `origin/main` · Verdict: **CONVERGED (round 0 decisive) — PR should NOT merge as-is**

Codex session: `019e95dd-0b94-7f02-b28c-5ec01407ca7f`. One round; round 0's attack was comprehensive and every gap was confirmable from the code (conceded), so the honest action is rework, not more rounds. Zero user-escalations (all gaps are objective code facts).

## Hardened understanding (post-grill)
The PR's framing ("deterministic guard + knowledge fix") does **not** hold:

1. **The `score_efficiency` lint is unwired.** `query_experiment_service.py:520` hardcodes `score_efficiency: 0.0` and the KEEP/REVERT decision uses `substring_score` only (`:565`, `:577`, `:597`). `query_eval_harness.score_efficiency()` is called **only by unit tests**. So the new penalties guard nothing in the live eval/nightly/benchmark loop.
2. **The "NEVER `BUILTIN.DF` in WHERE" rule + −0.3 penalty are over-broad and self-contradictory.** `netsuite.yaml:52` and `suiteql_agent.py:102` explicitly allow `BUILTIN.DF(field) = 'Value Name'` to filter small custom-list fields. The real problem was narrow — `BUILTIN.DF(sa.country)` on an **unbounded address join** — not all `BUILTIN.DF` in WHERE. The blanket rule false-positives on legitimate list-field filters.
3. **Inconsistent with the rest of the knowledge base.** The vs-MCP benchmark case `sales_country_canonical.yaml:24` still documents `WHERE BUILTIN.DF(sa.country) IN ('Norway',...)` as the verified-canonical query; the golden dataset `join-patterns-and-aggregation.md:159` still says both forms "work". Changing only `netsuite.yaml` leaves the agent learning the old pattern from these surfaces.
4. **Heuristic leaks** in the address-join penalty: `FETCH FIRST` is treated as a scan-bound but only limits returned *groups/rows* (an all-time `GROUP BY ... FETCH FIRST` still full-scans); the trandate-predicate test matches *any* `TRANDATE >=` anywhere (not tied to `t.trandate` or the address join); `BUILTIN.DF` in a `JOIN ... ON` or `HAVING` clause escapes the WHERE regex; `TRUNC(t.trandate) >= ...` is a false positive (TRANDATE followed by `)`).
5. **Global-profile pollution.** "`t.shipcountry` is NOT_EXPOSED **on this account**" is tenant-specific schema baked into the shared `netsuite.yaml` profile.
6. **Negligible magnitude.** −0.3 efficiency × 15% composite weight = 0.045 — and unused per (1).

**The actual production guard is the deferred #2 (live-execution latency/timeout gate).** This PR is prompt text + a fixture + an unwired, leaky scorer.

## Cross-exam transcript
### Round 0 — codex attack (verbatim, abridged to findings)
- "The scorer is not wired into the visible experiment/benchmark decision path… `query_experiment_service` imports only `EvalCase`… initializes `score_efficiency` to `0.0`, then decides KEEP/REVERT using `substring_score` only." → **conceded** (query_experiment_service.py:36/510/520/565).
- "`gq-031` validates fixture SQL, not agent SQL… It does not prove a live agent will avoid `BUILTIN.DF(sa.country)`." → **conceded** (test_golden_query_regression.py:69).
- FETCH FIRST limits groups not rows; `BUILTIN.DF` in `ON`/`HAVING` escapes; any `TRANDATE>=` marks bounded; `TRUNC(t.trandate)>=` false positive; blanket `BUILTIN.DF`-in-WHERE penalty hits legit list filters. → **all conceded** (query_eval_harness.py:159-161; netsuite.yaml:52).
- Prompt pollution not fixed: `sales_country_canonical.yaml:24`, `join-patterns-and-aggregation.md:159`, `suiteql_agent.py:102` still teach the old pattern. → **conceded.**
- "`t.shipcountry` NOT_EXPOSED on this account" in a global profile = tenant-specific schema applied globally. → **conceded.**
- Magnitude ~0.045, and the deferred latency gate "is not a follow-up polish item; it is the actual production guard." → **conceded.**

## Escalated to user
None — every gap was resolvable from the codebase.

## Recommended rework (so the guard is real, scoped, and consistent)
1. **Scope, don't blanket.** Restrict the rule + penalty to **address-table country filtering** (`BUILTIN.DF(sa.country|ba.country)` in WHERE), not all `BUILTIN.DF`. Reconcile with the list-field guidance instead of contradicting it.
2. **Wire it or drop it.** Either compute `score_efficiency` in the experiment/benchmark decision path (give it teeth), or remove the lint rather than ship an unwired guard that reads as protection. If kept, tie the address-join scope to a real `t.trandate` predicate (not `FETCH FIRST`, not any stray `TRANDATE`).
3. **Fix the canonical sources too.** Update `sales_country_canonical.yaml` (and the golden-dataset doc) to the raw-ISO + date-scoped pattern, or the agent keeps learning the slow one.
4. **De-pollute the profile.** Phrase shipcountry generically ("may be NOT_EXPOSED — verify via metadata"), not "on this account."
5. Keep `gq-031` as documentation of the correct query, but label it a *fixture* guard, not an agent guard.
6. **The real fix is #2** (live latency gate) — promote it from "follow-up" to the primary work.

---

## Rework + re-grill rounds (2026-06-04) — CONVERGED

User chose **option A (rework fully)**. Reworked across 5 commits (af1c99d, 02c8074,
1242029, f5fddbb, 340ff07) and re-grilled twice more (codex sessions
019e9622-…, plus round 3). Verdict progression:

- **Round 1 (post-rework):** "not fully closed" — 4 residuals: veto ran post-execution;
  a LIVE surface (`SuiteQLAgent._build_metadata_reference`) still taught the blanket
  `BUILTIN.DF(field)='<name>'`; `gq-031` over-forbade the bare DF substring (rejecting
  display use); detector regex leaks (NOT IN / reversed / aliasless / `<>` / comments).
  → all fixed in `f5fddbb`.
- **Round 2 (post-f5fddbb):** all 4 CLOSED. 2 NEW: a second live RAG seed
  (`netsuite_metadata_rag._format_custom_list_values`) lacked the caveat; single-level
  wrappers (`LOWER(BUILTIN.DF(sa.country))=…`) evaded the filter regex.
  → both fixed in `340ff07`.
- **Round 3 verdict:** F1–F4 CLOSED; the two round-2 gaps CLOSED.

### Accepted, documented residuals (NOT silently capped)
The static SQL-text detector is a best-effort heuristic and has documented evasions:
- Multi-arg wrappers (`NVL(BUILTIN.DF(sa.country), '')`) still bypass the filter regex.
- `_TRANDATE_PREDICATE` is alias-blind — a stray `<other>.trandate >=` satisfies the bound.

These are inherent to regex-on-SQL and are the architectural reason the **real backstop is
follow-up #2 — a live-execution latency/timeout gate** in the vs-MCP nightly worker
(`agent_benchmark_vs_mcp.py`; `case.max_latency_ms` is already plumbed but dead in
`run_vs_mcp.py`). The static lint is a cheap pre-filter for the common cases; the live gate
catches arbitrarily-shaped slow SQL.

### Net of the rework
- `score_efficiency` is now WIRED (was dead) as a **pre-execution promotion veto** in the
  autonomous-improvement loop — a timeout-prone candidate is never executed, benchmarked,
  or promoted.
- The country-filter heuristic is scoped + leak-hardened (no longer contradicts the
  small-list rule; catches NOT IN / reversed / aliasless / single-level wrappers).
- Every live BUILTIN.DF-country surface reconciled to the fast pattern: `netsuite.yaml`
  (×2 rules), `_build_metadata_reference`, `netsuite_metadata_rag`, `sales_country_canonical.yaml`,
  `join-patterns-and-aggregation.md`, `custom-fields-and-records.md`, `gq-031`.
- Profile de-polluted ("on this account" → generic).
