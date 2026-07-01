# grill-me — Sonnet 5 + adaptive-thinking migration (diff mode)

> Date: 2026-06-30 · Target: `feat/sonnet-5-adaptive-thinking` (PR #152) · Adversary: codex (gpt-5.5, xhigh) · **Verdict: CONVERGED** (2 rounds)

## Hardened understanding

Chat default → `claude-sonnet-5` with native **adaptive thinking + `output_config.effort`** (low→xhigh), replacing legacy `budget_tokens` (which 400s on Sonnet-5-class models). No SDK upgrade (anthropic 0.79.0 already ships `ThinkingConfigAdaptiveParam` + `output_config.effort`).

**Final invariants:**
- **Model-gated thinking** (`thinking.thinking_mode`): `adaptive` (Sonnet 5 / 4.6 / Opus 4.6+ / Fable) → `thinking:{adaptive}` + effort; `legacy` (4.5 / 4.0 / 4.1 / **Haiku**) → `budget_tokens` + `temperature=1`.
- **Effort is model-aware** (`anthropic_effort(level, model)`): `xhigh→"xhigh"` only on Sonnet 5 / Opus 4.7+ / Fable; on Sonnet 4.6 / Opus 4.6 (which lack xhigh but support max) `xhigh→"max"`. Sending `xhigh` to 4.6 400s.
- **Sonnet 5 thinks by default** → suppression (level none / forced `tool_choice` / kill-switch) on adaptive models sends `thinking:{disabled}` explicitly (omitting leaves it on; required for the kill-switch and the forced-tool 400 on plan-mode clarify). Legacy/Haiku stay omit (off by default).
- **One thinking mode per turn**: the loop-exhausted final hops now pass `current_thinking_level`, so there's no mid-turn adaptive→disabled toggle.
- **BYOK tenants keep their pinned model** (Framework on `claude-sonnet-4-6`, runs via the adaptive path — 4.6 supports adaptive+effort+disabled, and xhigh maps to max there). Non-BYOK → platform default (Sonnet 5).

## Cross-exam transcript

### Round 1 — 6 findings
- **R1#1 xhigh 400s on Sonnet 4.6 / Opus 4.6 — REAL** (would hit BYOK Framework via escalate→xhigh). Conceded → model-aware `anthropic_effort` (xhigh→max on 4.6).
- **R1#2 Haiku misclassified no-thinking — REAL.** Haiku supports `budget_tokens`, not effort → reclassified `legacy`; the `none` mode was removed.
- **R1#5 loop-exhausted final hop adaptive→disabled toggle — REAL.** Conceded → final hops pass `current_thinking_level`.
- **R1#3 deadline-only-on-text — DISMISSED** (pre-existing; the legacy path bumped `max_tokens` even higher (≤40960) and shipped; this diff caps lower at 32768).
- **R1#4 secondary agent paths run thinking-disabled — DISMISSED** (correct: protects those paths from the Sonnet-5-default footgun; enabling thinking there is a follow-up).
- **R1#6 forced-tool fix only unit-proven — UNCONFIRMED** (live-API confirmation = the vs-MCP benchmark + staging).
- Codex **cleared** the biggest worry: current Anthropic docs explicitly allow adaptive prior assistant turns *without* thinking blocks, and empty-text thinking blocks are preserved — so a skipped-thinking hop does not 400.

### Round 2 → CONVERGED
Verified all three fixes; no new surviving gap.

## Escalated to user
None.

## Open / follow-up (non-blocking)
- Secondary agent paths (workspace / onboarding / single-agent) don't pass a `thinking_level`, so they run thinking-disabled on Sonnet 5 (correct, but they don't get thinking's benefit). Optional enhancement.
- The per-hop 180s stream deadline isn't checked during a long no-text thinking phase (pre-existing); high/xhigh turns are bounded by the outer 300s turn timeout.

## Test state
Full backend suite after fixes: zero new failures (only the pre-existing `main` Redis flake). ruff clean. The pre-fix CI run (incl. **vs-MCP benchmark**) was all-green at default `med` effort.
