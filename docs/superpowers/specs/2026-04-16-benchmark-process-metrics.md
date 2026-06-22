# Design: Benchmark Process Metrics + Paraphrase Diversity + Retrieval Observability

Generated 2026-04-16 after the shipping-country regression (PR #45).
Status: DRAFT
Mode: Intrapreneurship (internal tooling)

## Problem Statement

The vs-MCP agent benchmark is green every day on 18 sales cases, yet on 2026-04-16 we shipped a user-facing regression that had been latent for six days. The agent was burning 10+ tool calls rediscovering the shipping-country join before either cancelling or limping to a right answer — and every single benchmark run graded that behavior as PASS, because the last tool call happened to succeed and the answer string contained the expected substrings.

The benchmark measures one thing: *did the agent eventually produce a right-looking answer?* It does not measure *cost, first-attempt correctness, tool-call efficiency, confidence stability, or paraphrase robustness*. Those are the things that turn "technically correct" into "the user trusts this." We shipped a regression because the one thing we measure was insensitive to the thing our users noticed.

This spec proposes five follow-ups — low-risk, additive to the existing benchmark — to close that observability gap.

## Demand Evidence

From PR #45's post-mortem:

- **2026-04-09 / 2026-04-10**: Olivia's country-sales session. Six patterns auto-learned. Queries answered correctly on the first tool call. Cost ~$0.07/query.
- **2026-04-10 to 2026-04-16**: no one re-ran the same query with a fresh phrasing. Benchmark cases kept passing — because they use Olivia's *exact* 2026-04-09 wording, which embeds close to the stored patterns.
- **2026-04-16**: user asked *"what are the 4 new countries we recently launched?"* — same intent, different phrasing. Patterns didn't match (similarity < 0.45). Agent rediscovered the join across 10+ tool calls, user cancelled the run. Cost ~$0.46/query.
- **PR #45 fix + re-run of the same canonical case on staging**: cost dropped $0.46 → $0.07 (7×), single tool call. But the benchmark would have called *both* runs a PASS — neither hit the bar in `scorer.py` for "bad result."

That's the shape of the hole. The benchmark is a lagging indicator of correctness and a blind indicator of everything else that matters to the user experience.

## Status Quo

**What the benchmark measures today** (`backend/tests/agent_benchmarks/scorer.py`):
- `expected_answer_contains`: string substring match on the assistant's final text (e.g. must contain "Switzerland").
- `expected_tools`: did the agent call at least one of the expected tools at some point.
- `expected_accuracy`: aggregated LLM-judge score.
- `max_cost` / `max_latency_ms`: soft ceilings per case (often not enforced as hard fails — or set so loosely they never trip).

**What it does NOT measure**:
- First-attempt tool-call correctness.
- Total tool calls per case (proxy for rediscovery loops).
- Similarity score of the matched pattern (or lack thereof).
- Paraphrase coverage (each intent has one canonical phrasing).
- `last_used_at` staleness on admin-seeded patterns.

## Target User & Narrowest Wedge

**Target:** the engineer who ships a chat/agent change. Today they see "CI green, benchmark green, ship it." The gap closes the day that dashboard makes them look twice at a run where accuracy is flat but tool calls doubled.

**Narrowest wedge:** three additive metrics in `scorer.py` + one new per-case variant file + one log line on every retrieval miss. No new infrastructure. Ships in one PR.

## Constraints

- **No regression on existing benchmark cases or thresholds.** The 18 existing `vs_mcp/*.yaml` files keep passing as-is. New metrics are additive.
- **No new LLM calls** beyond what the benchmark already makes. Paraphrase variants rerun the same agent; the tool count, cost, and first-attempt check are derived from the existing run's trace.
- **No DB schema changes.** The benchmark result table (`agent_benchmark_runs`) already stores per-case JSON; new metrics land inside that JSON blob.
- **CI gate remains `accuracy ≥ 16/18 wins vs MCP`.** The new metrics warn but don't block.

## Premises

1. **The benchmark can tell us "right answer" but not "right way."** Today's scorer operates on final text. To catch process regressions we have to score the *trace*: what tools were called, in what order, how many.
2. **Paraphrase brittleness is the hidden regression.** The canonical benchmark queries embed near their stored patterns by accident (same author, same session). Real users paraphrase. Without paraphrase variants, a passing benchmark tells us nothing about robustness.
3. **Retrieval misses are currently invisible.** `[PATTERN_RETRIEVAL] returned=0` is logged, but the nearest-miss similarity scores aren't. We can't distinguish "no candidate" from "candidate at 0.42 just under the 0.45 threshold" without that log line.
4. **Stale patterns are a leading indicator.** A seeded pattern with `last_used_at` more than N days old is either obsolete or stranded (user asks this intent a different way now). Either case is signal.
5. **Re-enabling some form of learning is the compounding fix.** Process metrics + paraphrase diversity + observability catch problems. They don't solve the underlying issue that our knowledge base is frozen at 2026-04-10. Eval-gated nightly promotion (already scoped in the `autonomous-improvement` skill) is the long-term answer. Out of scope for this spec but called out as follow-up.

## Approaches Considered

### Approach A: Just add metrics (minimal, rejected as standalone)
Add `tool_calls_before_first_success`, `first_attempt_correctness`, and `matched_pattern_similarity` to the scorer. Existing cases emit the new fields. Dashboards show the numbers. Done.
- **Effort:** S (~2-3 hours)
- **Risk:** Low.
- **Pros:** Ships fast, closes the process-blindness gap.
- **Cons:** Doesn't address paraphrase brittleness or retrieval observability. We'd still be blind to the "different phrasing same intent" regression.

### Approach B: Full observability rebuild (rejected as standalone)
Add metrics + paraphrase variants + nearest-miss logging + staleness dashboard + eval-gated auto-learning.
- **Effort:** L (1-2 weeks).
- **Risk:** Medium — auto-learning revival is the hard part; it's what got disabled in the first place.
- **Pros:** Full solution; catches every class of regression we know about today.
- **Cons:** Scope creep. The auto-learning piece is its own design effort.

### Approach C: Three-wave ship (CHOSEN)
Ship observability first, paraphrase variants second, auto-learning last.
- **Effort:** M (3-5 days, staged).
- **Risk:** Low for waves 1-2, Medium for wave 3.
- **Pros:** Fast feedback, staged risk, each wave provides independent value.
- **Cons:** Three PRs instead of one. Each wave's success depends on the prior one's data.

## Recommended Approach

**Approach C.** Ship in three waves.

### Wave 1 — Process metrics + retrieval observability (~1 day)

**1. Add three metrics to `scorer.py`** (`backend/tests/agent_benchmarks/scorer.py`):

- `tool_calls_total`: count of every tool call in the trace.
- `tool_calls_before_first_success`: number of tool calls before the first non-error response from a query tool. (Proxy for rediscovery loops — 1 = first-try success; 5+ = likely a loop.)
- `first_attempt_sql_correctness`: for SuiteQL/BQ cases, boolean — was the FIRST SQL emitted syntactically correct AND used the expected join/field pattern. Requires each case YAML to optionally declare `expected_first_sql_contains: [list of substrings]`.

Emit alongside existing fields in the result JSON. Dashboard (if any) surfaces them; CI doesn't gate on them yet (needs a baseline first).

**2. Add nearest-miss logging to `query_pattern_service.retrieve_similar_patterns`** (`backend/app/services/query_pattern_service.py`):

Today: `[PATTERN_RETRIEVAL] returned=0` on miss.
New: `[PATTERN_RETRIEVAL] returned=0 | top_candidates=[('sales data by shipping country', 0.42), ('sales by item class and shipping country', 0.38), ('revenue in USD by shipping country today', 0.37)]` when at least one candidate existed but scored below threshold. Only logs when `returned=0` — no overhead on the hit path.

This turns "no signal" into "the fix is lower threshold" vs. "the fix is new pattern" vs. "the fix is prompt-level teaching."

**3. Pattern staleness query** (runbook, not a DB change):

Document a SQL snippet in `docs/runbooks/pattern-staleness.md`:

```sql
SELECT tenant_id, user_question, last_used_at, CURRENT_DATE - last_used_at AS days_stale
FROM tenant_query_patterns
WHERE last_used_at < CURRENT_DATE - INTERVAL '7 days'
ORDER BY last_used_at ASC;
```

Optional next step: expose this as a metric on the benchmark dashboard. For now, a scheduled job that Slacks the list weekly is sufficient.

### Wave 2 — Paraphrase variants (~1 day)

**1. Add `paraphrases` field to benchmark case schema** (`backend/tests/agent_benchmarks/benchmark_cases/vs_mcp/*.yaml`):

```yaml
case_id: sales_country_canonical
query: "give me the sales data of Norway, Switzerland, New Zealand and Singapore as of today, sales data includes total sales order amount and system sales qty"
paraphrases:
  - "what are the 4 new countries we recently launched?"
  - "show me sales for NZ, CH, NO, SG"
  - "how are we doing in our newest shipping destinations"
  - "revenue from European countries outside our main markets"
  - "which countries did we ship to for the first time this month"
expected_answer_contains:
  - "Norway"
  - ...
```

**2. Runner executes each paraphrase as a distinct sub-case.** Sub-cases share the answer-correctness expectations but have their own tool-calls, cost, and latency. The summary table breaks out per-case success rate as `canonical: 1.0 | paraphrases: 3/5 (60%)`.

**3. Regression criterion: paraphrase success rate must be ≥ 80%.** Below that, the case FAILs the CI gate even if the canonical run passes. This forces every new pattern/prompt change to be robust across phrasings, not just the seeded one.

**4. Seed initial paraphrases from real transcripts** — grep the last 30 days of staging `chat_messages` for user queries that hit the same intent as each benchmark case's canonical query (cluster by embedding similarity). No LLM-generated paraphrases in the initial wave; we want the tests grounded in actual usage.

### Wave 3 — Eval-gated pattern promotion (~1 week, separate design)

Referenced here for completeness but out of scope for this spec. The `autonomous-improvement` skill has the existing scaffolding. Design requirements:

- Patterns admitted from live chat sessions ONLY after passing their own paraphrase-diverse benchmark check.
- New patterns tagged with `source = "eval_promoted"` to distinguish from `"admin_seed"`.
- Reversion path: a demoted pattern (one that starts failing its paraphrase check on a future run) moves to `is_active=false` rather than being deleted, with audit trail.
- Slack notification when a pattern is promoted OR demoted — this becomes the team's compounding-learning dashboard.

## Cross-Model Perspective

*Not yet solicited. If this design advances past draft, get an independent read from a Claude subagent (or Codex) before implementing Wave 1.*

## Open Questions

1. **Dashboard or raw numbers?** Process metrics without a dashboard are hard to monitor. Do we already have something consuming `agent_benchmark_runs` table, or does this spec need to propose a minimal frontend/query view?
2. **Who owns the paraphrase corpus?** Seeding from real transcripts is fine for Wave 2, but ongoing maintenance (adding variants for new intents, retiring stale ones) needs an owner. Engineering? A designated "benchmark steward" rotating role?
3. **Threshold for `first_attempt_sql_correctness`.** What substrings constitute "correct" varies by case. For shipping-country it's `sa.nKey = t.shippingAddress`. For subsidiary queries it's different. Case YAMLs must declare this, and we need a starter set.
4. **Should Wave 1 gate CI, or just warn?** Recommending WARN for the first month (collect baselines) and GATE after that once we have a sense of normal vs. regression.
5. **What about BigQuery cases?** Patterns are NetSuite-only today. Wave 2 paraphrase variants work for BigQuery too (test framework is source-agnostic), but Wave 3 (eval-gated learning) requires `tenant_query_patterns` to extend to BigQuery patterns — a separate schema/service change.

## Success Criteria

- **Wave 1 success (1 day):** Every benchmark run emits the three new metrics. Nearest-miss logging visible in staging when `returned=0`. Pattern staleness query runs against staging DB and returns rows for expected stale patterns (e.g., the 6 shipping-country patterns with `last_used_at = 2026-04-10`).
- **Wave 2 success (1 day):** `sales_country_canonical` has 5 paraphrase variants. Running the benchmark against PR #45 HEAD shows ≥ 80% paraphrase success rate. Running it against the pre-PR-#45 code (via `git checkout`) shows < 80% — proving the test actually catches the regression.
- **Wave 3 success (1 week):** One pattern auto-promoted via the eval-gated pipeline; visible in Slack; traceable via `source` column. `agent_benchmark_runs` shows no regression after promotion.
- **Quantitative north star:** same vs-MCP benchmark must hold at ≥ 16 wins out of 18 throughout all three waves. New metrics add observability; they never lower the bar.

## Distribution Plan

Existing pipeline. Each wave is a separate PR against main → CI → staging deploy via `saas-deployment`. No new infrastructure, no new secrets, no Alembic migrations for Wave 1 or 2. Wave 3 may add a `patterns_source` column (Alembic migration) and a Slack webhook secret.

## Dependencies

- Existing `backend/tests/agent_benchmarks/` harness (scorer, runner, persistence).
- Existing `tenant_query_patterns` table (migration 034).
- Existing `query_pattern_service.py` retrieval path.
- Wave 3 depends on the `autonomous-improvement` skill scaffolding (already scoped).
- No external dependencies. No new packages.

## The Assignment

Before starting Wave 1 implementation: pick one real staging chat session from the last 7 days where the agent took more than 5 tool calls, export its trace, and hand-compute each proposed metric. That exercise will surface edge cases in the metric definitions (What counts as a "success"? What if the first tool call is `bigquery_schema` before the actual SQL?) better than any spec review. Two hours of work, saves days of rework.

## What I noticed about the way we got here

- The shipping-country regression wasn't a bug; it was a latent gap that only surfaces when a user asks a question differently than the person who originally taught the system. The test suite was never built to catch that class of problem, because the test suite was seeded from the same corpus it defends.
- You noticed the regression by *using* the product, not by looking at the dashboard. That's the fastest honest feedback loop we have today. The dashboard caught up to your observation; it didn't lead you to it.
- Every metric in this spec exists to shorten the gap between "the user has noticed" and "the engineer has noticed." Not to make the benchmark prettier.
