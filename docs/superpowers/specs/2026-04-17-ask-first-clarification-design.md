# Ask-First Clarification + Regression Capture — Design Spec

**Date:** 2026-04-17
**Status:** Draft
**Author:** agent-pair (Aiden + Claude)

## Problem

Users notice the unified agent has *stopped asking clarifying questions* on ambiguous requests. Specific regression reported on 2026-04-17 staging: query "sales analysis" → agent picks a source and runs a tool call immediately, no clarification offered. In v1.x the source picker card (two tiles — NetSuite vs BigQuery) would render when confidence was low. That structured output was deleted in PR #40 (v2.0 knowledge-driven unified agent) along with the entire coordinator/source-picker classifier layer.

The deletion was correct in direction: the old `source_picker` was a pre-agent regex + Haiku classifier — hardcoded routing that v2.0 replaced with knowledge profiles and model self-routing. We do NOT want to revive the classifier.

But the *clarifying-question behavior* wasn't just a property of the classifier; it was a property the product needed. Tearing out the classifier also tore out the UX.

Separately, the user notes "there might be more" regressions — this is a *class* of issue. Every time we delete a subsystem we risk losing a behavior we wanted to keep. We need a pattern to codify good behaviors as permanent regression guards, not just fix this one.

## Goals

1. **Restore ask-first behavior** on genuinely ambiguous requests without adding any pre-agent classifier. The unified agent itself decides when to ask.
2. **Make the clarifying question visually loud** in chat so users notice and engage. No new frontend card needed if markdown is sufficient.
3. **Capture this behavior as a regression guard.** Any future agent change that stops asking in these cases fails CI.
4. **Establish a pattern** for turning "we noticed a regression" into a permanent benchmark case, generalizing beyond this one instance.

## Non-Goals

- **No classifier, no routing layer, no coordinator.** Ask-first is agent-initiated via prompt policy, not gated by a pre-agent model.
- **No new frontend card or structured_output type in v1 of this work.** Plain markdown first; if still too subtle, add a `<clarify>...</clarify>` post-stream extractor as a follow-up.
- **No change to `source_pin` semantics.** It remains a prompt hint. When a pin is set, the agent should honor it silently rather than re-ask source.
- **Not a broader clarification toolkit** (e.g., entity disambiguation, multi-step wizard). Scope is source + period + dimension clarification via one prompt block.

## Design

### Layer 1: Prompt policy in the unified agent base

Add a new `<clarification_policy>` XML block to `_SYSTEM_PROMPT` in `backend/app/services/chat/agents/unified_agent.py`, sitting alongside `<tool_selection>`. The block specifies *when* to ask, *when not to*, and *how* to format the ask.

**When to ask:**
- Query is genuinely ambiguous — multiple valid interpretations that would return materially different results.
- Source is ambiguous AND the tenant has multiple relevant connectors (NetSuite + BigQuery) AND no `source_pin` is set.
- Dimension is ambiguous ("sales analysis" without a breakdown dimension when there are several reasonable defaults).
- Period is ambiguous ("sales" with no time window and no obvious default from conversation context).

**When NOT to ask:**
- Intent is reasonably clear even if some parameters defaulted (e.g., "sales last week" → period clear, run it).
- `source_pin` is set → honor the pin silently, don't re-ask source. Still ask about other ambiguities.
- Follow-up to a prior query where context disambiguates.
- Single-connector tenant where source is trivially determined.
- Lookup-style queries ("SO865732") where there's nothing to ask.

**How to ask (format):**
Use a markdown blockquote with a bold lead and bulleted options. Max ~4 bullets. Each bullet is a question OR a short choice list.

```
> **Before I pull this — a few things to check:**
> - Which period? (e.g., Jan 1 – today, last 30 days, Q1 2026)
> - By what dimension? (country, subsidiary, item class, customer)
> - NetSuite or BigQuery? (NetSuite = transactional truth; BigQuery = Shopify/Heap/attribution)
```

Max one clarification turn per user request — if the user's first follow-up still leaves things ambiguous, pick a reasonable default and execute rather than looping.

### Layer 2: Benchmark regression guards

Extend the vs-MCP benchmark harness to support a new expected-behavior verdict: `ask_first`.

**New case type in `backend/app/services/benchmarks/benchmark_cases/vs_mcp/`:**
```yaml
name: sales_analysis_ambiguous
query: "show me sales analysis"
tenant_id: ce3dfaad-626f-4992-84e9-500c8291ca0a
connectors: [netsuite, bigquery]  # both connected — source is ambiguous
source_pin: null
expected_behavior: ask_first
expected_question_about: [period, dimension, source]  # at least one of these
```

**Scorer extension** in `backend/app/services/benchmarks/scorer.py`:
- When `expected_behavior: ask_first`, the scorer checks:
  - (a) Turn produced **zero** tool calls.
  - (b) Turn produced a response containing the callout format (blockquote + bold lead, or at minimum a question mark + enumerated options).
  - (c) LLM judge confirms the clarifying question addresses at least one item in `expected_question_about`.
- When `expected_behavior: must_not_ask_first`, the scorer checks:
  - (a) Turn produced **at least one** tool call OR a direct answer without an enumerated question list.
  - (b) Response does NOT contain the callout format.
- Pass = all checks for the verdict type. Fail = any check fails. Both verdicts reuse the existing LLM judge infra for the rationale field.

**Initial cases (5):**
1. `sales_analysis_ambiguous_source` — both connectors, no pin, no hints → expect source question.
2. `sales_no_period` — NS only, no period stated → expect period question.
3. `sales_no_dimension` — NS only, period clear, dimension unstated → expect dimension question.
4. `sales_with_pin_respects_pin` — source_pin=bigquery, ambiguous period → agent should NOT re-ask source, SHOULD ask period.
5. `lookup_no_ask` (counter-case) — "show me SO865732" → expect ZERO clarification, direct tool call. Scorer verdict `must_not_ask_first`. This guards against over-asking.

These lock in the behavior. Any agent/profile change that regresses one of them fails the nightly benchmark and the PR CI gate.

### Layer 3: Pattern for "captured regressions"

Document the process so future captures are mechanical:

1. Regression reported → reproduce on staging → identify the query + expected behavior.
2. Write a benchmark case (prefer `ask_first` or an `expected_query`/`expected_tool` scorer).
3. Add to `benchmark_cases/vs_mcp/` — PR CI now enforces it.
4. Fix the underlying cause (prompt tweak, profile update, tool description).
5. Benchmark turns green.

Runbook for this at `docs/runbooks/capturing-behavior-regressions.md` (created alongside this work).

## File map

| File | Op | Purpose |
|---|---|---|
| `backend/app/services/chat/agents/unified_agent.py` | Modify | Add `<clarification_policy>` block to `_SYSTEM_PROMPT` |
| `backend/app/services/benchmarks/scorer.py` | Modify | Add `ask_first` verdict path + LLM judge adapter |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_analysis_ambiguous_source.yaml` | Create | Case |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_period.yaml` | Create | Case |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_dimension.yaml` | Create | Case |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_with_pin_respects_pin.yaml` | Create | Case |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/lookup_no_ask.yaml` | Create | Counter-case (prevent over-asking) |
| `backend/tests/test_unified_agent_clarification_policy.py` | Create | Substring assertions: policy block exists, has "When to ask" + "How to ask" + the callout format example |
| `backend/tests/test_benchmark_ask_first_scorer.py` | Create | Unit tests for the `ask_first` scorer path (no LLM, deterministic mocks of response shapes) |
| `backend/tests/test_prompt_trim.py` | Modify | Bump `_SYSTEM_PROMPT` ceiling from 13000 → ~14000 to fit the new block (~500 chars) |
| `docs/runbooks/capturing-behavior-regressions.md` | Create | Playbook — how to turn a reported regression into a permanent guard |

## Out of scope (explicit follow-ups)

- **Post-stream `<clarify>` extractor + frontend callout card** — punt to v2 if plain markdown proves insufficient after 1 week of use. The extractor would mirror the `<chart>` pattern.
- **Entity disambiguation** (which customer? which subsidiary?) — related but separate. Already partially handled by `tenant_resolver.py` NER + fuzzy match. Not in this spec.
- **Multi-turn wizard** (for truly complex asks) — no evidence we need it yet.
- **`_INVESTIGATION_RE` coverage audit + `seed_tenant_patterns.py`** — remains PR B per the Phase 2 spec, separate work.

## Success criteria

1. `sales_analysis_ambiguous_source` case passes on staging (Framework tenant, both connectors) — agent asks before tool-calling.
2. Nightly vs-MCP benchmark includes the 4 new cases. They pass on the current branch.
3. Baseline benchmark suite (18 existing cases) does not regress — accuracy stays at or above current levels.
4. Front-end subjective: clarifying questions render visibly enough that a casual user notices (validated by user testing on staging after deploy).
5. Runbook exists and is discoverable — captured in CLAUDE.md skills index or memory.

## Risk / trade-offs

- **Over-asking.** If the prompt policy is too eager, the agent asks on queries the user considered obvious. Mitigation: "When NOT to ask" section in the policy is explicit. LLM judge in benchmarks catches over-ask via a counter-case ("show me SO865732" → expect zero-question behavior).
- **Markdown loudness.** Blockquote + bold may still feel subtle if the surrounding prose is verbose. If this falls out of user testing, add the extractor + card as a follow-up.
- **Scorer brittleness.** The `ask_first` scorer relies on detecting the callout format. If the LLM drifts the format, the scorer false-negatives. Mitigation: scorer also accepts any response with zero tool calls + question mark + enumerated options (looser fallback).
- **Prompt bloat.** Adding ~500 chars to `_SYSTEM_PROMPT` erodes the ceiling tightening from Phase 2 PR A. Net position: base prompt rises from 6752 → ~7300 chars, still well under new 14000 ceiling.

## Related

- Phase 2 PR A (#48, merged 2026-04-17): moved SuiteQL rules into netsuite.yaml. This work respects that split — clarification policy is agent-global, not NS-specific.
- PR #40 (knowledge-driven agent): deleted source_picker. This spec does not revive it.
- Source pin: `backend/app/services/chat/prompt_assembler.py::build_source_pin_hint` — interacts with the "When NOT to ask" rule.
