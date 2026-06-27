# grill-me — adaptive agentic thinking remediation (diff mode)

> Date: 2026-06-27 · Target: `feat/adaptive-agentic-thinking-glm` review-remediation diff (3f63ee6..HEAD) · Adversary: codex (gpt-5.5, xhigh) · **Verdict: CONVERGED** (3 rounds)

## Hardened understanding

The diff adds adaptive "thinking" to the chat agent (per-turn `thinking_level` →
Anthropic `budget_tokens` on SDK 0.79.0), an OpenRouter adapter foundation, and an
agent-callable `escalate_reasoning` tool. It remediates a T2 multi-angle review whose
verifiers were rate-limited. Codex cross-examined the remediation across 3 rounds.

**Final, hardened invariants:**

- **Extended thinking is incompatible with a forced `tool_choice` (type tool/any) → 400.**
  Forcing only ever happens at step 0 (`step_tool_choice = tool_choice if step == 0 else None`).
  Therefore a forced-tool turn runs **thinking-OFF for the whole turn** — pinned in
  `base_agent.run()`/`run_streaming()` via `thinking.is_forced_tool_choice(tool_choice)` —
  not just on the forced hop, because re-enabling thinking at step 1 against the blockless
  step-0 history would 400. The adapter still suppresses per-hop as defense-in-depth.
- **`escalate_reasoning` only RAISES depth when thinking is genuinely active**
  (`thinking.budget_for(current_thinking_level) > 0`) — never flips none→on mid-turn, respects
  the `CHAT_THINKING_ENABLED` kill-switch, and is robust to a misconfigured default level
  (budget 0 but not the literal string `"none"`).
- **Trivial-turn thinking suppression keeps the financial/importance exclusions:** a
  lookup-shaped financial query ("what is gross profit") is deliberately kept off Haiku
  because it needs care, so it must KEEP thinking. `_thinking_is_trivial` mirrors the
  Haiku-routing predicate minus the BYOK exclusion (so BYOK trivial turns also skip thinking)
  plus chitchat.
- **GLM / China-origin models are not exposed.** `VALID_MODELS["openrouter"]` is US-models-only
  (`openai/gpt-4o-mini`); the dead Layer-2 GLM apparatus + residency-guard config were removed.
  OpenRouter provider pins are `{data_collection: deny, zdr: true}` (no open-weight-host
  allowlist, which would have excluded OpenAI and broken routing for the only exposed model).
- **SDK reality:** anthropic 0.79.0 — adaptive thinking / `output_config.effort` do not exist;
  `budget_tokens` + `temperature=1` is correct/functional for every allowed model (none are
  Opus 4.7/4.8). The review's "deprecated-shape-400" finding was a forward-compat note, not live.

## Cross-exam transcript

### Round 1 — 12 findings
- **R1#1 forced-tool step0→step1 thinking 400 — REAL (latent).** Conceded → fixed (turn-wide pin).
- **R1#2** — codex corrected Claude's premise: `_task_contains_query` does NOT force `tool_choice`
  (it appends a corrective user message). The "common query turn" was never exposed. Accepted.
- **R1#3 test gap — REAL.** Added `is_forced_tool_choice` unit tests + adapter suppression tests.
- **R1#4 persisted GLM config bypass — re-raised in R3, REBUTTED** (see below).
- **R1#5 OpenRouter pin excluded OpenAI — REAL.** Conceded → fixed (drop `only` allowlist).
- **R1#6** reasoning_effort on gpt-4o-mini — UNCONFIRMED, OpenRouter not wired to chat. Deferred.
- **R1#7–#12** pre-existing repo architecture (MCP-HITL exact-match, learned-rules prompt
  pollution, soul injection, MCP tool descriptions, tenant defense-in-depth, metric suppression).
  Not introduced by this diff — out of scope, noted for separate hardening.

### Round 2 — 3 findings
- **R2#1 financial/important lookup got thinking suppressed — REAL.** Conceded → fixed
  (`_thinking_is_trivial` keeps financial/high-importance thinking on).
- **R2#2 misconfigured default level lets escalate flip thinking on — REAL (edge).** Conceded →
  fixed (escalate guard keys on `budget_for(...) > 0`).
- **R2#3** confirmed the round-1 forced-tool pin closes the 400 for valid levels.

### Round 3 — 1 finding → CONVERGED
- **R3#1 persisted OpenRouter GLM config bypasses the strip — REBUTTED.** Exposure is null: the
  config endpoint could only accept `z-ai/glm-5.2` against `VALID_MODELS`, which only became
  possible on this unmerged branch (build commit 6f832b3). Production never had GLM in
  `VALID_MODELS`; the branch never deployed; so no GLM BYOK config exists anywhere to bypass.
  The underlying "BYOK runtime trusts persisted model without re-validating against
  `VALID_MODELS`" is a pre-existing pattern for ALL providers, not introduced by this diff;
  runtime revalidation is a separate hardening that risks breaking legitimate BYOK tenants on
  unlisted models. No new in-scope surviving gap → **CONVERGED** (also at the 3-round cap).

## Escalated to user
None — every surviving finding was resolvable from code/facts.

## Open gaps (future hardening, NOT blockers for this diff)
- BYOK `get_tenant_ai_config` does not revalidate the persisted model against `VALID_MODELS`
  (pre-existing, all providers). Consider a runtime allowlist check or a China-origin denylist
  when the GLM tier is properly wired.
- OpenRouter `reasoning_effort` is sent for non-reasoning models (e.g. gpt-4o-mini); gate by
  model capability when OpenRouter is actually wired into the chat path.
- Pre-existing repo concerns R1#7–#12 (MCP-HITL exact-match, learned-rules pollution, soul
  length/escaping, MCP tool-description pollution) — separate workstreams.

## Test state
Full backend suite after all grill fixes: **3763 passed**, 17 skipped, 882 errors (environmental
`socket.gaierror` — DB-less sandbox), 1 failed = pre-existing `test_acquire_lock_prevents_concurrent_sync`
(Redis, fails identically on `main`). Zero new failures from the remediation. ruff clean.
