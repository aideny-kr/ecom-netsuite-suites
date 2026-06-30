# Adaptive Agentic Thinking on the Chat Path (+ gated GLM-5.2 tier)

> Design spec — 2026-06-25
> Branch: `feat/adaptive-agentic-thinking-glm`
> ClickUp: [OpenRouter integration to lower compute cost](https://app.clickup.com/t/86baku1yf) (P0)
> Tier: **T2** (key-billed chat · prompt-pollution surface · feature flags · benchmark-gated)

## Problem

The chat path uses a strong model the user is happy with (`claude-sonnet-4-6` via per-tenant BYOK), but it has **no native test-time reasoning enabled**. Every "reasoning"/"thinking" reference in the codebase is prompt-induced `<reasoning>…</reasoning>` XML that the model writes in normal output tokens and which is then stripped before display (`orchestrator.py:448`, `base_agent.py:377`). The Anthropic adapter's actual API calls pass only `model` + `max_tokens` + `tools` + `system` — **no `thinking={...}` parameter anywhere**. So the agent gets zero real reasoning compute today.

We want:
1. **Automatic** adaptive thinking — the model spends reasoning compute in proportion to turn difficulty, with **no hardcoded Python difficulty router** (respects `prefer_model_intelligence_over_routing`).
2. **Agent-orchestrated** escalation — the agent itself can request heavier reasoning horsepower on hard turns.
3. A path to **GLM-5.2** as a cheap, powerful thinking engine — without violating the data-residency rule (`china_hosted_models_synthetic_only`), since GLM is China-origin and the chat path is the #1 customer-data surface.

## Non-goals (this spec)

- **Internal-surface cost routing** (gpt-4o-mini on the eval/judge/confidence Haiku surfaces) — sibling follow-up spec under the same ticket. The OpenRouter adapter built here is the shared dependency it will reuse.
- **Enabling GLM-5.2 on real customer traffic** — built but physically blocked; unlock is a separate, gated decision (see Workstream 3).
- Replacing the model the user likes. Sonnet 4.6 stays the default; thinking is layered on.

## Approach overview

Three workstreams, in dependency order:

1. **Adaptive thinking engine on Sonnet 4.6** — independent, highest value, lowest risk. Depends only on the existing Anthropic adapter.
2. **OpenRouter adapter foundation** — shared infra; unblocks the GLM tier (and later the internal-surface routing).
3. **GLM-5.2 thinking tier** — flagged + physically blocked behind a residency guard + benchmark gate.

The orchestration model (decided): **Layer 1 self-regulation + Layer 2 agent escalation tool.**

---

## Workstream 1 — Adaptive thinking engine

### 1.1 Provider-agnostic thinking level at the adapter interface

`BaseLLMAdapter.create_message()` / `stream_message()` gain an optional `thinking_level: ThinkingLevel` where `ThinkingLevel ∈ {none, low, med, high, xhigh}`. Each adapter maps it to its native parameter; the orchestrator never speaks a provider's dialect:

| Level | Anthropic (`budget_tokens`) | OpenRouter/GLM (`reasoning_effort`) |
|-------|------------------------------|--------------------------------------|
| none  | thinking omitted             | reasoning omitted                    |
| low   | ~2k                          | low                                  |
| med   | ~6k                          | medium                               |
| high  | ~12k                         | high                                 |
| xhigh | ~24k                         | xhigh (max)                          |

(Budgets are starting values, tunable via settings — see 1.5.)

### 1.2 Anthropic adapter changes

When `thinking_level != none`, build the request with:
- `thinking={"type": "enabled", "budget_tokens": N}`
- `temperature = 1` (Anthropic **requires** temperature=1 with extended thinking — override any default)
- `max_tokens > budget_tokens` (ensure the cap leaves room for the answer after thinking)

**Stream handling:** the adapter must now parse `thinking` content blocks (a new block type alongside `text` / `tool_use`):
- Preserve thinking blocks and pass them back across tool-use turns — Anthropic **requires** the prior `thinking` block be echoed when continuing after a `tool_use` in a thinking-enabled turn. Dropping it breaks the turn.
- Surface a lightweight **"thinking…"** indicator to the UI via the existing SSE stream, but **never stream the raw chain-of-thought content** to the client.

The ≤60s read-timeout override and retry logic are unchanged (extended thinking does not change the timeout contract; the outer `asyncio.wait_for(300s)` still caps the turn).

### 1.3 Layer 1 — automatic self-regulation

- Native thinking is **always enabled** on the chat model with a **generous default budget** (e.g. `med`/`high` budget). `budget_tokens` is a **cap, not a target** — the model spends little on easy turns, more on hard ones, on its own. This is the "triggers automatically" behavior with **zero routing code**.
- **Exception:** simple-lookup turns already shunt to Haiku via `_is_simple_lookup()` (`orchestrator.py`). Those run at `thinking_level=none` — single-entity "show me order X" lookups should not burn thinking tokens. This is the *one* place a deterministic gate is acceptable, because it reuses an existing classification, not a new heuristic.

### 1.4 Layer 2 — agent-orchestrated escalation

- Add an `escalate_reasoning` tool (a.k.a. `deep_think`) to the unified agent's tool inventory. No arguments required beyond an optional short `rationale`.
- When the model calls it mid-turn, the orchestrator **bumps `thinking_level`** for the continuation of that turn (`high → xhigh`, and — once unlocked — routes the continuation to the GLM-5.2 tier). The agent's existing multi-step tool loop (`base_agent.py`) is the natural carrier; no new control flow.
- Registered through the tool inventory + category registry so the capability-sync CI invariant (`test_prompt_tool_sync.py`) stays green and the `{{TOOL_INVENTORY}}` placeholder picks it up (no hardcoded tool names — `tool_capability_sync`).

### 1.5 Reconcile with existing pseudo-`<reasoning>` prompt tags

With native thinking on, the prompt instruction to emit `<reasoning>` blocks is redundant and risks double-reasoning. When `thinking_level != none`, **drop the `<reasoning>` instruction** from the assembled prompt.
- This touches the unified-agent prompt under the **prompt↔profile-YAML sync invariant** (`CLAUDE.md` common mistake #3). The `<reasoning>`-stripping regex (`orchestrator.py:448`, `base_agent.py:377`) stays as a belt-and-suspenders cleanup.
- Any change must be mirrored verbatim in affected `knowledge_profiles/*.yaml`, never paraphrased.

### 1.6 Settings

`backend/app/core/config.py`:
- `CHAT_THINKING_DEFAULT_LEVEL: str = "med"` — Layer-1 default level.
- `CHAT_THINKING_BUDGETS: dict` (or per-level env knobs) — token budgets per level.
- `CHAT_THINKING_ENABLED: bool = True` — global kill-switch for the whole feature.

---

## Workstream 2 — OpenRouter adapter foundation

### 2.1 Adapter

`OpenRouterAdapter(OpenAIAdapter)` in `backend/app/services/chat/adapters/openrouter_adapter.py` — OpenRouter is OpenAI-API-compatible, so we **subclass and override only**:
- `base_url = "https://openrouter.ai/api/v1"`
- api key from `OPENROUTER_API_KEY`
- attribution headers (`HTTP-Referer`, `X-Title`)
- **provider-routing pins**: US providers only + `zdr: true` + `data_collection: "deny"` (passed via OpenRouter's `provider` request field / `extra_body`)
- `reasoning_effort` mapping for `thinking_level` (per 1.1)

Inherited unchanged: tool-format conversion, `force_tool_choice`, ≤60s timeout override, retry/backoff.

### 2.2 Registry + factory

- `get_adapter()` in `llm_adapter.py` gains an `openrouter` branch.
- `VALID_PROVIDERS` += `openrouter`; `VALID_MODELS["openrouter"]` += the GLM ids we permit (e.g. `z-ai/glm-5.2`, `z-ai/glm-5`); `DEFAULT_MODELS["openrouter"]`.

### 2.3 Settings

- `OPENROUTER_API_KEY: str = ""` — **env only**, never a shell export (`anthropic_key_billing_leak`).

---

## Workstream 3 — GLM-5.2 thinking tier (flagged + physically blocked)

### 3.1 Config

- `CHAT_THINKING_MODEL: str = ""` / `CHAT_THINKING_PROVIDER: str = ""` — when set, the escalated (Layer-2) tier routes to this model/provider instead of bumping the tenant's own model. Default empty → escalation just raises the native thinking level on the tenant's existing model.
- Feature flag `chat_glm_thinking` (tenant_feature_flags) — **default OFF**.
- Hard guard `ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA: bool = False` — a China-origin provider (GLM) **cannot** be selected for a customer-data turn unless this is explicitly `True`. The flag alone is insufficient; both must be set. This is the physical block.

### 3.2 BYOK respect

BYOK tenants (Framework, Rails on `claude-sonnet-4-6`) keep their own model. The GLM thinking tier applies only when a tenant is explicitly opted in; BYOK choice is never silently overridden.

### 3.3 Unlock conditions (encoded, not vibes)

GLM-5.2 reaches customer data only when **all** hold:
1. **Residency policy sign-off** — explicit decision (and `memory/` rule update) that US-hosted open-weights GLM with ZDR satisfies the customer-data residency requirement.
2. **Claude+MCP benchmark pass** — GLM-5.2 must match-or-beat the baseline on the standard gate (`benchmark_vs_claude_mcp`).
3. **Tool-calling-under-reasoning validation** — GLM's reasoning+tool-calling was unreliable in the 4.x line; verify on our actual agent loop before trusting it on tool-heavy chat turns.

Until then, the code path exists and is tested, but the guard returns the native-thinking fallback.

---

## Data flow (happy path, post-build, GLM still locked)

```
user msg → orchestrator
  ├─ simple lookup?  → Haiku, thinking_level=none
  └─ otherwise       → Sonnet 4.6, thinking_level=CHAT_THINKING_DEFAULT_LEVEL (always-on)
                          model self-paces within budget   ← Layer 1
                          │
                          └─ model calls escalate_reasoning → orchestrator bumps to xhigh
                                                              (→ GLM-5.2 tier IF unlocked) ← Layer 2
```

## Testing (TDD — write failing tests first)

- `thinking_level` → native-param mapping, per adapter (Anthropic `budget_tokens`+`temperature=1`; OpenRouter `reasoning_effort`).
- Anthropic stream: `thinking` block parsed, preserved, and round-tripped across a `tool_use` continuation.
- Layer 1: simple-lookup turn ⇒ `thinking_level=none`; normal turn ⇒ default level.
- Layer 2: `escalate_reasoning` tool present in inventory (capability-sync stays green); calling it bumps the level for the continuation.
- Prompt reconcile: when thinking enabled, assembled prompt omits the `<reasoning>` instruction; profile YAML stays in sync.
- OpenRouter adapter: base_url, attribution headers, US-provider+ZDR pins present; timeout ≤120s (`test_adapter_timeouts.py` pattern).
- GLM guard: requesting GLM on a customer-data turn **without** `ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA` returns the native fallback, not GLM.
- Adapter mocks follow the existing `patch(f"{_ORCH}.get_adapter", ...)` pattern (`test_chat_multi_provider.py`).

## Risks / open questions

- **Latency:** extended thinking adds wall-clock. Default level (`med`) chosen to balance; `CHAT_THINKING_ENABLED` is the kill-switch. Validate p50/p95 turn latency stays within the 300s budget under always-on thinking.
- **Cost:** always-on thinking raises output tokens. This is the tension with the cost-reduction ticket — mitigated by self-pacing (cheap turns stay cheap) + Haiku/simple-lookup exclusion. Track $/turn before/after.
- **Benchmark:** enabling thinking is itself a chat-path change → must clear the Claude+MCP gate even on Sonnet (Workstream 1), independent of GLM.
- **Prompt-sync invariant:** the `<reasoning>` reconcile is the highest-risk edit; needs the verbatim YAML mirror.

## Tiering

**T2.** Gates: existing CI + seeded-tenant e2e, safe-envelope live smoke, blocking multi-angle review pre-merge (`code-review-multiangle`), and the Claude+MCP benchmark gate. Independent-model grill before PR-ready.
