# Adaptive Agentic Thinking on the Chat Path (+ gated GLM-5.2 tier) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the chat agent real, automatic test-time reasoning (native extended thinking, self-paced per turn) plus an agent-callable `escalate_reasoning` tool, and lay an OpenRouter adapter + flag-and-residency-guarded GLM-5.2 thinking tier behind it.

**Architecture:** A provider-agnostic `thinking_level` (`none|low|med|high|xhigh`) flows through `BaseLLMAdapter` → each adapter maps it natively (Anthropic `budget_tokens`+`temperature=1`; OpenRouter `reasoning_effort`). The orchestrator sets the initial level per turn (`none` for simple-lookup/Haiku turns, default otherwise — Layer 1 self-regulation), and the agent loop bumps the level when the model calls `escalate_reasoning` (Layer 2). GLM-5.2 is reachable only through a default-off flag plus a hard `ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA` guard.

**Tech Stack:** Python 3.11, FastAPI, `anthropic` + `openai` async SDKs, pytest. Tests run via `backend/.venv/bin/python -m pytest`.

**Tier:** T2 (key-billed chat · prompt-pollution surface · feature flags · benchmark-gated). Gates at end of plan.

**Spec:** `docs/superpowers/specs/2026-06-25-adaptive-agentic-thinking-glm-design.md`

---

## File Structure

**New files:**
- `backend/app/services/chat/thinking.py` — `ThinkingLevel` constants, budget map, `next_level()` escalation helper, `budget_for()`. One responsibility: the level↔budget vocabulary.
- `backend/app/services/chat/adapters/openrouter_adapter.py` — `OpenRouterAdapter(OpenAIAdapter)`.
- `backend/tests/test_thinking_levels.py`, `backend/tests/test_anthropic_thinking.py`, `backend/tests/test_escalate_reasoning.py`, `backend/tests/test_openrouter_adapter.py`, `backend/tests/test_glm_thinking_guard.py`.

**Modified files:**
- `backend/app/services/chat/llm_adapter.py` — `thinking_level` on interface; `LLMResponse.thinking_blocks`; OpenRouter in factory + registries.
- `backend/app/services/chat/adapters/anthropic_adapter.py` — map level→thinking kwargs; capture/round-trip thinking blocks.
- `backend/app/services/chat/agents/base_agent.py` — thread + bump `thinking_level`; special-case `escalate_reasoning`.
- `backend/app/services/chat/orchestrator.py` — compute initial `thinking_level`; pass to agent.
- `backend/app/services/chat/tools.py` — `escalate_reasoning` tool definition.
- `backend/app/services/chat/tool_categories.py` — `escalate_reasoning` category.
- `backend/app/core/config.py` — thinking + OpenRouter + GLM-guard settings.
- `backend/tests/test_prompt_tool_sync.py` — register `escalate_reasoning` as a known tool.

---

# PHASE A — Adaptive thinking engine on Sonnet 4.6 (independent, ships first)

## Task A1: Thinking-level vocabulary

**Files:**
- Create: `backend/app/services/chat/thinking.py`
- Test: `backend/tests/test_thinking_levels.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_thinking_levels.py
from app.services.chat import thinking


def test_budget_for_known_levels():
    assert thinking.budget_for("none") == 0
    assert thinking.budget_for("low") == 2048
    assert thinking.budget_for("med") == 6144
    assert thinking.budget_for("high") == 12288
    assert thinking.budget_for("xhigh") == 24576


def test_budget_for_unknown_level_is_zero():
    assert thinking.budget_for("bogus") == 0
    assert thinking.budget_for(None) == 0


def test_next_level_escalates_one_step():
    assert thinking.next_level("none") == "med"
    assert thinking.next_level("low") == "high"
    assert thinking.next_level("med") == "high"
    assert thinking.next_level("high") == "xhigh"


def test_next_level_caps_at_xhigh():
    assert thinking.next_level("xhigh") == "xhigh"


def test_reasoning_effort_mapping():
    assert thinking.reasoning_effort("none") is None
    assert thinking.reasoning_effort("low") == "low"
    assert thinking.reasoning_effort("med") == "medium"
    assert thinking.reasoning_effort("high") == "high"
    assert thinking.reasoning_effort("xhigh") == "xhigh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_thinking_levels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.chat.thinking'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/chat/thinking.py
"""Provider-agnostic thinking-level vocabulary.

A `thinking_level` is one of none|low|med|high|xhigh. Each adapter maps it to
its native parameter (Anthropic budget_tokens; OpenAI/OpenRouter reasoning_effort).
This module owns ONLY the vocabulary so the mapping lives in one place.
"""

ThinkingLevel = str  # one of LEVELS

LEVELS: tuple[str, ...] = ("none", "low", "med", "high", "xhigh")

# Anthropic extended-thinking budget_tokens per level. 0 == thinking disabled.
_BUDGETS: dict[str, int] = {
    "none": 0,
    "low": 2048,
    "med": 6144,
    "high": 12288,
    "xhigh": 24576,
}

# OpenAI/OpenRouter reasoning_effort per level. None == omit the param.
_EFFORT: dict[str, str] = {
    "low": "low",
    "med": "medium",
    "high": "high",
    "xhigh": "xhigh",
}

# Escalation: one step up, capped at xhigh. "low" jumps to "high" so an explicit
# escalate from a shallow base makes a meaningful difference.
_NEXT: dict[str, str] = {
    "none": "med",
    "low": "high",
    "med": "high",
    "high": "xhigh",
    "xhigh": "xhigh",
}


def budget_for(level: str | None) -> int:
    """Anthropic budget_tokens for a level (0 = thinking off)."""
    return _BUDGETS.get(level or "", 0)


def reasoning_effort(level: str | None) -> str | None:
    """OpenAI/OpenRouter reasoning_effort for a level (None = omit)."""
    return _EFFORT.get(level or "")


def next_level(level: str | None) -> str:
    """One escalation step up, capped at xhigh."""
    return _NEXT.get(level or "", "high")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_thinking_levels.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/thinking.py backend/tests/test_thinking_levels.py
git commit -m "feat(chat): provider-agnostic thinking-level vocabulary [T2]"
```

---

## Task A2: Thinking config settings

**Files:**
- Modify: `backend/app/core/config.py` (Settings class, after `DEFAULT_AI_PROVIDER` at line 43)
- Test: `backend/tests/test_thinking_config.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_thinking_config.py
from app.core.config import Settings


def test_thinking_defaults():
    s = Settings()
    assert s.CHAT_THINKING_ENABLED is True
    assert s.CHAT_THINKING_DEFAULT_LEVEL == "med"


def test_thinking_default_level_is_a_valid_level():
    from app.services.chat import thinking

    s = Settings()
    assert s.CHAT_THINKING_DEFAULT_LEVEL in thinking.LEVELS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_thinking_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'CHAT_THINKING_ENABLED'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/core/config.py`, immediately after the line `DEFAULT_AI_PROVIDER: str = "anthropic"` (line 43), add:

```python
    # ── Adaptive thinking (chat path) ───────────────────────────────────────
    # Native extended reasoning. Always-on with a generous default budget so the
    # model self-paces depth per turn (Layer 1). CHAT_THINKING_ENABLED is the
    # global kill-switch. Levels: none|low|med|high|xhigh (see chat/thinking.py).
    CHAT_THINKING_ENABLED: bool = True
    CHAT_THINKING_DEFAULT_LEVEL: str = "med"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_thinking_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/tests/test_thinking_config.py
git commit -m "feat(chat): thinking enable + default-level settings [T2]"
```

---

## Task A3: `thinking_level` through the adapter interface + Anthropic mapping

This is the highest-risk task: extended thinking changes the Anthropic request (budget, `temperature=1`, larger `max_tokens`) AND the response (a `thinking` content block that MUST be echoed back across tool-use turns or the next call 400s).

**Files:**
- Modify: `backend/app/services/chat/llm_adapter.py:26-87` (add `thinking_blocks` to `LLMResponse`; add `thinking_level` to interface)
- Modify: `backend/app/services/chat/adapters/anthropic_adapter.py:147-339`
- Test: `backend/tests/test_anthropic_thinking.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_anthropic_thinking.py
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
from app.services.chat.llm_adapter import LLMResponse


def _block(btype, **kw):
    b = types.SimpleNamespace(type=btype)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _fake_message(content, in_tok=10, out_tok=20):
    usage = types.SimpleNamespace(
        input_tokens=in_tok, output_tokens=out_tok,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return types.SimpleNamespace(content=content, usage=usage)


@pytest.mark.asyncio
async def test_thinking_level_med_sets_budget_temperature_and_maxtokens():
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-4-6", max_tokens=16384, system="s",
        messages=[{"role": "user", "content": "hi"}], thinking_level="med",
    )

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 6144}
    assert captured["temperature"] == 1
    assert captured["max_tokens"] > 6144  # must exceed budget


@pytest.mark.asyncio
async def test_thinking_level_none_omits_thinking():
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-4-6", max_tokens=16384, system="s",
        messages=[{"role": "user", "content": "hi"}], thinking_level="none",
    )

    assert "thinking" not in captured
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_thinking_blocks_captured_and_round_tripped():
    adapter = AnthropicAdapter(api_key="sk-test")
    content = [
        _block("thinking", thinking="let me reason", signature="sig123"),
        _block("text", text="the answer"),
    ]
    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(return_value=_fake_message(content))

    resp: LLMResponse = await adapter.create_message(
        model="claude-sonnet-4-6", max_tokens=16384, system="s",
        messages=[{"role": "user", "content": "q"}], thinking_level="high",
    )

    # Captured
    assert resp.thinking_blocks == [
        {"type": "thinking", "thinking": "let me reason", "signature": "sig123"}
    ]
    # Round-tripped FIRST in the assistant message (Anthropic requires this)
    assistant = adapter.build_assistant_message(resp)
    assert assistant["content"][0] == {
        "type": "thinking", "thinking": "let me reason", "signature": "sig123"
    }
    assert assistant["content"][1] == {"type": "text", "text": "the answer"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_anthropic_thinking.py -v`
Expected: FAIL — `create_message() got an unexpected keyword argument 'thinking_level'`

- [ ] **Step 3: Write minimal implementation**

3a. In `backend/app/services/chat/llm_adapter.py`, extend `LLMResponse` (lines 26-30) to carry thinking blocks:

```python
@dataclass
class LLMResponse:
    text_blocks: list[str] = field(default_factory=list)
    tool_use_blocks: list[ToolUseBlock] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    thinking_blocks: list[dict] = field(default_factory=list)
```

3b. In the same file, add `thinking_level` to BOTH abstract signatures (`create_message` lines 37-47 and `stream_message` lines 50-60) — append this parameter after `tool_choice` in each:

```python
        thinking_level: str | None = None,
```

And in the default `stream_message` body (lines 66-74), forward it:

```python
        response = await self.create_message(
            model=model,
            max_tokens=max_tokens,
            system=system,
            system_dynamic=system_dynamic,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            thinking_level=thinking_level,
        )
```

3c. In `backend/app/services/chat/adapters/anthropic_adapter.py`, add a module-level helper after the imports (after line 13):

```python
from app.services.chat import thinking as _thinking


def _apply_thinking(kwargs: dict, max_tokens: int, thinking_level: str | None) -> None:
    """Mutate `kwargs` to enable Anthropic extended thinking for this level.

    Anthropic requires: temperature=1 when thinking is enabled, and
    max_tokens strictly greater than budget_tokens (the budget is part of the
    max_tokens allowance). We reserve `max_tokens` on top of the budget so the
    answer still has its full original room.
    """
    budget = _thinking.budget_for(thinking_level)
    if budget <= 0:
        return
    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
    kwargs["temperature"] = 1
    kwargs["max_tokens"] = budget + max_tokens
```

Add a helper to extract thinking blocks (also after line 13):

```python
def _extract_thinking_blocks(content) -> list[dict]:
    """Pull thinking / redacted_thinking blocks out of a message's content,
    preserving signatures — required to echo them back across tool-use turns."""
    blocks: list[dict] = []
    for block in content:
        if block.type == "thinking":
            blocks.append(
                {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
            )
        elif block.type == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": block.data})
    return blocks
```

Now update `create_message` (lines 147-205): add `thinking_level: str | None = None,` to the signature (after `tool_choice`), and after `kwargs` is built but before the `await` (i.e. right before line 183), insert:

```python
        _apply_thinking(kwargs, max_tokens, thinking_level)
```

Then in the block-extraction loop (lines 188-192), thinking blocks pass through harmlessly (they're not `text`/`tool_use`). After the loop, capture them — change the `return LLMResponse(...)` (lines 201-205) to:

```python
        return LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
            thinking_blocks=_extract_thinking_blocks(response.content),
        )
```

Update `stream_message` (lines 207-321) identically: add `thinking_level: str | None = None,` to the signature; insert `_apply_thinking(kwargs, max_tokens, thinking_level)` right before the retry loop (before line 245 `deadline = ...`); and capture thinking blocks into the final response (change lines 316-321):

```python
        response = LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
            thinking_blocks=_extract_thinking_blocks(final_message.content),
        )
        yield "response", response
```

3d. Update `build_assistant_message` (lines 326-339) to prepend thinking blocks FIRST (Anthropic requires the prior thinking block to lead the assistant turn when continuing after tool use):

```python
    def build_assistant_message(self, response: LLMResponse) -> dict:
        content: list[dict] = []
        # Thinking blocks MUST come first when present (required for tool-use
        # continuation in a thinking-enabled turn).
        content.extend(response.thinking_blocks)
        for text in response.text_blocks:
            content.append({"type": "text", "text": text})
        for tool in response.tool_use_blocks:
            content.append(
                {
                    "type": "tool_use",
                    "id": tool.id,
                    "name": tool.name,
                    "input": tool.input,
                }
            )
        return {"role": "assistant", "content": content}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_anthropic_thinking.py backend/tests/test_adapter_timeouts.py -v`
Expected: PASS (new thinking tests + existing timeout tests still green)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/llm_adapter.py backend/app/services/chat/adapters/anthropic_adapter.py backend/tests/test_anthropic_thinking.py
git commit -m "feat(chat): Anthropic extended-thinking via thinking_level + block round-trip [T2]"
```

---

## Task A4: Thread `thinking_level` through the agent loop + escalation hook

`base_agent.py` runs the multi-step tool loop. It must accept an initial `thinking_level`, pass it on every adapter call, and bump it when the model calls `escalate_reasoning` (handled in Task A5's dispatch; here we add the carrier).

**Files:**
- Modify: `backend/app/services/chat/agents/base_agent.py` — `run()` (~550) and `run_streaming()` (~869) signatures + call sites (627-635, 945-952)
- Test: `backend/tests/test_agent_thinking_threading.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_agent_thinking_threading.py
import pytest

from app.services.chat.llm_adapter import LLMResponse


class _RecordingAdapter:
    """Minimal adapter that records thinking_level on each stream call."""

    def __init__(self):
        self.levels: list[str | None] = []

    async def stream_message(self, **kwargs):
        self.levels.append(kwargs.get("thinking_level"))
        resp = LLMResponse(text_blocks=["done"])
        yield "text", "done"
        yield "response", resp

    def build_assistant_message(self, response):
        return {"role": "assistant", "content": [{"type": "text", "text": "done"}]}

    def build_tool_result_message(self, tool_results):
        return {"role": "user", "content": tool_results}


@pytest.mark.asyncio
async def test_run_streaming_passes_thinking_level_to_adapter():
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent()
    adapter = _RecordingAdapter()

    # Drive a single no-tool turn; assert the level we passed reached the adapter.
    gen = agent.run_streaming(
        task="hello",
        context={},
        db=None,
        adapter=adapter,
        model="claude-sonnet-4-6",
        thinking_level="high",
    )
    async for _ in gen:
        pass

    assert adapter.levels and adapter.levels[0] == "high"
```

> NOTE to implementer: if `UnifiedAgent.run_streaming` needs more context to reach the adapter call without DB, mock the minimal collaborators (`build_all_tool_definitions`, prompt assembly) the same way `test_chat_multi_provider.py` does, OR assert at the lowest-level helper that issues the adapter call. The invariant under test: the `thinking_level` argument reaches `adapter.stream_message`.

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_agent_thinking_threading.py -v`
Expected: FAIL — `run_streaming()` got an unexpected keyword argument `thinking_level`

- [ ] **Step 3: Write minimal implementation**

3a. Add `thinking_level: str | None = None,` to the `run()` signature (after `tool_choice`, ~line 558) and to `run_streaming()` (after `tool_choice`, ~line 876).

3b. Inside each method, establish a mutable local the loop reads, near the top of the method body:

```python
        current_thinking_level = thinking_level
```

3c. At the non-streaming call site (lines 627-635), add the kwarg:

```python
            response: LLMResponse = await adapter.create_message(
                model=model,
                max_tokens=16384,
                system=prompt_parts.static,
                system_dynamic=prompt_parts.dynamic,
                messages=messages,
                tools=tools,
                tool_choice=step_tool_choice,
                thinking_level=current_thinking_level,
            )
```

3d. At the streaming call site (lines 945-953), add the kwarg:

```python
                async for event_type, payload in adapter.stream_message(
                    model=model,
                    max_tokens=16384,
                    system=prompt_parts.static,
                    system_dynamic=prompt_parts.dynamic,
                    messages=messages,
                    tools=tools,
                    tool_choice=step_tool_choice,
                    thinking_level=current_thinking_level,
                ):
```

> The bump (`current_thinking_level = thinking.next_level(current_thinking_level)`) is wired in Task A5 where `escalate_reasoning` is dispatched.

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_agent_thinking_threading.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/agents/base_agent.py backend/tests/test_agent_thinking_threading.py
git commit -m "feat(chat): thread thinking_level through agent loop call sites [T2]"
```

---

## Task A5: `escalate_reasoning` tool — definition, category, dispatch, bump

The tool is synthetic (control signal, not a data tool). It is NOT routed to `mcp_server`; the agent loop special-cases it like `reference_previous_result`, bumps `current_thinking_level`, and returns a short confirmation tool-result.

**Files:**
- Modify: `backend/app/services/chat/tools.py` — add the tool definition to `build_local_tool_definitions()` (~line 49) and a special-case in `execute_tool_call()` (~line 216)
- Modify: `backend/app/services/chat/tool_categories.py` — add to `_EXACT` (line ~27)
- Modify: `backend/app/services/chat/agents/base_agent.py` — detect the call in the tool loop and bump the level
- Modify: `backend/tests/test_prompt_tool_sync.py` — register the known tool name
- Test: `backend/tests/test_escalate_reasoning.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_escalate_reasoning.py
from app.services.chat import thinking
from app.services.chat.tool_categories import categorize
from app.services.chat.tools import build_local_tool_definitions


def test_escalate_reasoning_tool_is_advertised():
    names = {t["name"] for t in build_local_tool_definitions()}
    assert "escalate_reasoning" in names


def test_escalate_reasoning_schema_has_optional_rationale():
    tool = next(t for t in build_local_tool_definitions() if t["name"] == "escalate_reasoning")
    props = tool["input_schema"]["properties"]
    assert "rationale" in props
    assert tool["input_schema"].get("required", []) == []  # no required args


def test_escalate_reasoning_categorized():
    assert categorize("escalate_reasoning") == "control"


def test_next_level_used_for_bump():
    # The loop bumps via thinking.next_level — assert the contract it relies on.
    assert thinking.next_level("med") == "high"
    assert thinking.next_level("high") == "xhigh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_escalate_reasoning.py -v`
Expected: FAIL — `escalate_reasoning` not advertised / category not `control`

- [ ] **Step 3: Write minimal implementation**

3a. In `backend/app/services/chat/tool_categories.py`, add a `"control"` category to the `Category` type if it is a `Literal` (check the top of the file and extend the literal union to include `"control"`), then add to `_EXACT`:

```python
    "escalate_reasoning": "control",
```

3b. In `backend/app/services/chat/tools.py`, define the synthetic tool and append it inside `build_local_tool_definitions()` just before `return tools` (~line 73):

```python
    tools.append(
        {
            "name": "escalate_reasoning",
            "description": (
                "Call this when the current question needs deeper, more careful "
                "reasoning than a quick answer — multi-step logic, ambiguous "
                "requirements, reconciling conflicting data, or tricky SuiteQL. "
                "Calling it increases your reasoning depth for the rest of this "
                "turn. Use it sparingly, only when genuinely warranted."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "One short phrase on why deeper reasoning is needed.",
                    }
                },
                "required": [],
            },
        }
    )
```

3c. In `execute_tool_call()` (`tools.py` ~line 216), add a special-case at the very top (alongside the `reference_previous_result` branch), so it never routes to `mcp_server`:

```python
    if tool_name == "escalate_reasoning":
        # Control signal handled by the agent loop (it bumps thinking depth).
        # Returning a terse ack keeps the tool-result contract intact.
        return json.dumps({"ok": True, "message": "Reasoning depth increased for this turn."})
```

3d. In `base_agent.py`, where tool calls are iterated and dispatched (the loop that calls `execute_tool_call`), detect the escalation BEFORE/around dispatch and bump the carried level. Add, inside the per-tool-call handling:

```python
                if tool_call.name == "escalate_reasoning":
                    current_thinking_level = thinking.next_level(current_thinking_level)
```

Add the import at the top of `base_agent.py`:

```python
from app.services.chat import thinking
```

3e. In `backend/tests/test_prompt_tool_sync.py`, add `"escalate_reasoning"` to the known-tool set in `_all_known_tool_names_for_tenant_with_every_connector()` so the capability-sync invariant recognises it.

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_escalate_reasoning.py backend/tests/test_prompt_tool_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/tools.py backend/app/services/chat/tool_categories.py backend/app/services/chat/agents/base_agent.py backend/tests/test_escalate_reasoning.py backend/tests/test_prompt_tool_sync.py
git commit -m "feat(chat): escalate_reasoning tool — agent-orchestrated thinking bump (Layer 2) [T2]"
```

---

## Task A6: Orchestrator sets the initial `thinking_level` (Layer 1)

`none` for simple-lookup/Haiku turns (they should not burn thinking tokens); `CHAT_THINKING_DEFAULT_LEVEL` otherwise; honour the `CHAT_THINKING_ENABLED` kill-switch. Then pass it into the agent.

**Files:**
- Modify: `backend/app/services/chat/orchestrator.py` — after the `unified_model`/Haiku selection (lines 2855-2867), compute `thinking_level`; pass it to the `run_streaming`/`run` call
- Test: `backend/tests/test_orchestrator_thinking_level.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_orchestrator_thinking_level.py
from app.services.chat.orchestrator import compute_thinking_level


def test_simple_lookup_gets_none():
    assert compute_thinking_level(is_simple_lookup=True, enabled=True, default="med") == "none"


def test_normal_turn_gets_default():
    assert compute_thinking_level(is_simple_lookup=False, enabled=True, default="med") == "med"


def test_kill_switch_forces_none():
    assert compute_thinking_level(is_simple_lookup=False, enabled=False, default="high") == "none"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_orchestrator_thinking_level.py -v`
Expected: FAIL — `cannot import name 'compute_thinking_level'`

- [ ] **Step 3: Write minimal implementation**

3a. Add a pure helper near the top of `orchestrator.py` (next to `_is_simple_lookup`, ~line 120):

```python
def compute_thinking_level(*, is_simple_lookup: bool, enabled: bool, default: str) -> str:
    """Layer-1 initial thinking level. Simple lookups (Haiku) never think;
    the global kill-switch forces none; otherwise use the configured default."""
    if not enabled or is_simple_lookup:
        return "none"
    return default
```

3b. In `run_chat_turn()`, right after the Haiku-routing block (after line 2867), compute the level. Reuse the `_is_simple_lookup(sanitized_input)` result rather than recomputing semantics:

```python
    _thinking_is_simple = unified_model == HAIKU_MODEL
    turn_thinking_level = compute_thinking_level(
        is_simple_lookup=_thinking_is_simple,
        enabled=settings.CHAT_THINKING_ENABLED,
        default=settings.CHAT_THINKING_DEFAULT_LEVEL,
    )
```

3c. Pass `thinking_level=turn_thinking_level` into the `agent.run_streaming(...)` (and any `agent.run(...)`) call in `run_chat_turn`. (Search for `run_streaming(` / `.run(` in `run_chat_turn` and add the kwarg.)

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_orchestrator_thinking_level.py -v`
Expected: PASS

- [ ] **Step 5: Run the chat regression suite (no regressions)**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_chat_multi_provider.py backend/tests/test_orchestrator_paths.py -v`
Expected: PASS (existing chat orchestration still green)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/chat/orchestrator.py backend/tests/test_orchestrator_thinking_level.py
git commit -m "feat(chat): Layer-1 auto thinking level per turn (none for simple lookups) [T2]"
```

---

## Task A7: Reconcile the redundant `<reasoning>` prompt instruction (prompt-sync–guarded)

With native thinking on, the prompt instruction to emit `<reasoning>` blocks (`unified_agent.py:240`: "Output reasoning in a `<reasoning>` block (hidden from user).") is redundant and risks double-reasoning. Make it conditional. The `<reasoning>`-stripping regex stays as belt-and-suspenders.

> ⚠️ Touches the unified-agent prompt under the prompt↔profile-YAML sync invariant (`chat-orchestration.md` #5, #20). Keep any wording change mirrored in the relevant `knowledge_profiles/*.yaml`. This task is **deferrable** — if it threatens the prompt-sync test under time pressure, ship Phase A without it (the regex already strips stray `<reasoning>` output) and track as a follow-up.

**Files:**
- Modify: `backend/app/services/chat/agents/unified_agent.py:240` (and wherever the static prompt is assembled to allow conditional omission)
- Test: `backend/tests/test_reasoning_prompt_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_reasoning_prompt_reconcile.py
from app.services.chat.agents.unified_agent import build_reasoning_instruction


def test_reasoning_instruction_dropped_when_thinking_on():
    assert build_reasoning_instruction(thinking_enabled=True) == ""


def test_reasoning_instruction_present_when_thinking_off():
    text = build_reasoning_instruction(thinking_enabled=False)
    assert "<reasoning>" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_reasoning_prompt_reconcile.py -v`
Expected: FAIL — `cannot import name 'build_reasoning_instruction'`

- [ ] **Step 3: Write minimal implementation**

Extract the `<reasoning>` instruction sentence from the inline prompt at `unified_agent.py:240` into a helper, and call it with `thinking_enabled` (derive from `settings.CHAT_THINKING_ENABLED`). Add:

```python
def build_reasoning_instruction(*, thinking_enabled: bool) -> str:
    """The pseudo-CoT instruction is only needed when native thinking is OFF.
    With extended thinking enabled the model reasons natively, so we omit it to
    avoid double-reasoning."""
    if thinking_enabled:
        return ""
    return "Output reasoning in a <reasoning> block (hidden from user)."
```

Replace the hardcoded line in the prompt assembly with the helper's output. Mirror the wording change in the corresponding `knowledge_profiles/*.yaml` fragment if it duplicates this instruction (grep `knowledge_profiles` for `<reasoning>`).

- [ ] **Step 4: Run test + the prompt-sync invariant**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_reasoning_prompt_reconcile.py backend/tests/test_prompt_tool_sync.py -v`
Expected: PASS (sync invariant stays green)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/agents/unified_agent.py backend/tests/test_reasoning_prompt_reconcile.py
git commit -m "feat(chat): drop redundant <reasoning> instruction when native thinking on [T2]"
```

**Phase A checkpoint:** `backend/.venv/bin/python -m pytest backend/tests/ -k "thinking or escalate or adapter or prompt_tool_sync or multi_provider" -v` — all green. Phase A is independently shippable: adaptive thinking on Sonnet 4.6 with agent-orchestrated escalation.

---

# PHASE B — OpenRouter adapter foundation

## Task B1: `OpenRouterAdapter`

OpenRouter is OpenAI-API-compatible → subclass `OpenAIAdapter`, override base_url + key + attribution headers + US-provider/ZDR pins + `reasoning_effort` mapping.

**Files:**
- Create: `backend/app/services/chat/adapters/openrouter_adapter.py`
- Test: `backend/tests/test_openrouter_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_openrouter_adapter.py
from app.services.chat.adapters.openrouter_adapter import OpenRouterAdapter


def test_base_url_points_at_openrouter():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    assert "openrouter.ai/api/v1" in str(adapter._client.base_url)


def test_timeout_is_non_default():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    assert adapter._client.timeout.read <= 120
    assert adapter._client.timeout.connect <= 10


def test_provider_pins_are_us_and_zdr():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    pins = adapter._provider_pins()
    assert pins["data_collection"] == "deny"
    assert pins["zdr"] is True
    assert isinstance(pins.get("only"), list) and pins["only"]  # US-host allowlist


def test_reasoning_effort_threaded_into_extra_body():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    body = adapter._extra_body(thinking_level="high")
    assert body["reasoning_effort"] == "high"
    assert body["provider"]["zdr"] is True


def test_reasoning_omitted_for_none():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    body = adapter._extra_body(thinking_level="none")
    assert "reasoning_effort" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_openrouter_adapter.py -v`
Expected: FAIL — `No module named 'app.services.chat.adapters.openrouter_adapter'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/chat/adapters/openrouter_adapter.py
"""OpenRouter adapter — OpenAI-API-compatible gateway.

Subclasses OpenAIAdapter (OpenRouter speaks the OpenAI Chat Completions API) and
overrides only the base_url, key, attribution headers, provider-routing pins
(US hosts + Zero-Data-Retention), and reasoning_effort threading.

RESIDENCY: provider pins restrict routing to US-hosted endpoints with ZDR. China
-origin models (e.g. GLM) are still gated separately and MUST NOT reach customer
data without ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA (see config + glm guard).
"""

import httpx
import openai

from app.services.chat.adapters.openai_adapter import OpenAIAdapter
from app.services.chat import thinking as _thinking

_CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=60.0)
_CLIENT_MAX_RETRIES = 2
_BASE_URL = "https://openrouter.ai/api/v1"

# US-hosted providers we permit OpenRouter to route to. Tighten/loosen here.
_US_PROVIDER_ALLOWLIST = ["DeepInfra", "Together", "Fireworks", "Baseten"]


class OpenRouterAdapter(OpenAIAdapter):
    def __init__(self, api_key: str):
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=_BASE_URL,
            timeout=_CLIENT_TIMEOUT,
            max_retries=_CLIENT_MAX_RETRIES,
            default_headers={
                "HTTP-Referer": "https://suitestudio.ai",
                "X-Title": "Suite Studio",
            },
        )

    def _provider_pins(self) -> dict:
        """OpenRouter `provider` routing constraints: US hosts + ZDR + no logging."""
        return {"only": list(_US_PROVIDER_ALLOWLIST), "data_collection": "deny", "zdr": True}

    def _extra_body(self, *, thinking_level: str | None) -> dict:
        body: dict = {"provider": self._provider_pins()}
        effort = _thinking.reasoning_effort(thinking_level)
        if effort is not None:
            body["reasoning_effort"] = effort
        return body

    async def create_message(self, *, thinking_level: str | None = None, **kwargs):
        # OpenAI SDK forwards unknown params via extra_body; inject provider pins
        # + reasoning_effort there so the parent's request building is untouched.
        kwargs["extra_body"] = self._extra_body(thinking_level=thinking_level)
        return await super().create_message(**kwargs)

    async def stream_message(self, *, thinking_level: str | None = None, **kwargs):
        kwargs["extra_body"] = self._extra_body(thinking_level=thinking_level)
        async for ev in super().stream_message(**kwargs):
            yield ev
```

> Implementer note: the parent `OpenAIAdapter.create_message`/`stream_message` build their own `kwargs` dict for `chat.completions.create`. Pass `extra_body` through by having the parent merge any `extra_body` it receives into its final `kwargs` (add `if extra_body: kwargs["extra_body"] = extra_body` in the parent, accepting `extra_body: dict | None = None`). Make that one-line parent change in this task and assert via `test_openrouter_adapter.py` that `reasoning_effort`/`provider` land in the outgoing body (mock `_client.chat.completions.create` and capture kwargs, mirroring `test_chat_multi_provider`).

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_openrouter_adapter.py backend/tests/test_adapter_timeouts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/adapters/openrouter_adapter.py backend/app/services/chat/adapters/openai_adapter.py backend/tests/test_openrouter_adapter.py
git commit -m "feat(chat): OpenRouter adapter (US-pinned, ZDR, reasoning_effort) [T2]"
```

---

## Task B2: Register OpenRouter in factory + registries + config

**Files:**
- Modify: `backend/app/services/chat/llm_adapter.py` — `get_adapter` (133-148), `VALID_PROVIDERS` (96), `VALID_MODELS`/`DEFAULT_MODELS` (90-130)
- Modify: `backend/app/core/config.py` — `OPENROUTER_API_KEY`
- Test: `backend/tests/test_openrouter_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_openrouter_registry.py
from app.services.chat.llm_adapter import (
    DEFAULT_MODELS, VALID_MODELS, VALID_PROVIDERS, get_adapter,
)
from app.services.chat.adapters.openrouter_adapter import OpenRouterAdapter


def test_openrouter_is_a_valid_provider():
    assert "openrouter" in VALID_PROVIDERS
    assert "z-ai/glm-5.2" in VALID_MODELS["openrouter"]
    assert DEFAULT_MODELS["openrouter"]


def test_factory_returns_openrouter_adapter():
    adapter = get_adapter("openrouter", "sk-or-test")
    assert isinstance(adapter, OpenRouterAdapter)


def test_openrouter_api_key_setting_exists():
    from app.core.config import Settings
    assert hasattr(Settings(), "OPENROUTER_API_KEY")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_openrouter_registry.py -v`
Expected: FAIL — `openrouter` not in `VALID_PROVIDERS`

- [ ] **Step 3: Write minimal implementation**

3a. In `llm_adapter.py`: add to `DEFAULT_MODELS` (line 90-94) `"openrouter": "z-ai/glm-5.2",`; add `"openrouter"` to `VALID_PROVIDERS` (line 96); add to `VALID_MODELS`:

```python
    "openrouter": [
        "z-ai/glm-5.2",
        "z-ai/glm-5",
        "openai/gpt-4o-mini",
    ],
```

3b. In `get_adapter` (133-148), add before the `else`:

```python
    elif provider == "openrouter":
        from app.services.chat.adapters.openrouter_adapter import OpenRouterAdapter

        return OpenRouterAdapter(api_key=api_key)
```

3c. In `config.py`, after `ANTHROPIC_MODEL` (line 42), add:

```python
    # OpenRouter gateway — env only, never a shell export (key-billing leak risk).
    OPENROUTER_API_KEY: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_openrouter_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/llm_adapter.py backend/app/core/config.py backend/tests/test_openrouter_registry.py
git commit -m "feat(chat): register openrouter provider + GLM models + OPENROUTER_API_KEY [T2]"
```

---

# PHASE C — GLM-5.2 thinking tier (flagged + physically blocked)

## Task C1: Residency-guard config + GLM-tier settings

**Files:**
- Modify: `backend/app/core/config.py` — `CHAT_THINKING_MODEL`, `CHAT_THINKING_PROVIDER`, `ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA`
- Test: `backend/tests/test_glm_guard_config.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_glm_guard_config.py
from app.core.config import Settings


def test_glm_tier_defaults_are_safe():
    s = Settings()
    assert s.CHAT_THINKING_MODEL == ""        # empty → escalate on tenant's own model
    assert s.CHAT_THINKING_PROVIDER == ""
    assert s.ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA is False  # blocked by default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_glm_guard_config.py -v`
Expected: FAIL — attribute missing

- [ ] **Step 3: Write minimal implementation**

In `config.py`, after the thinking settings from Task A2, add:

```python
    # ── Escalated (Layer-2) thinking tier ───────────────────────────────────
    # When set, escalate_reasoning routes the continuation to this model/provider
    # instead of just raising the native thinking level on the tenant's model.
    # Empty → escalate on the tenant's own model. GLM (z-ai/*) is China-origin:
    # it CANNOT serve a customer-data turn unless the hard guard below is True.
    CHAT_THINKING_MODEL: str = ""
    CHAT_THINKING_PROVIDER: str = ""
    ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_glm_guard_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/tests/test_glm_guard_config.py
git commit -m "feat(chat): GLM-tier config + ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA guard (default off) [T2]"
```

---

## Task C2: GLM-tier selection helper with the residency block

A pure function decides whether an escalated turn may route to the GLM tier. China-origin (`z-ai/`) requires the guard flag; otherwise it returns the native fallback (escalate on the tenant's own model). This is the physical block, unit-tested in isolation.

**Files:**
- Create helper in `backend/app/services/chat/thinking.py` (append)
- Test: `backend/tests/test_glm_thinking_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_glm_thinking_guard.py
from app.services.chat.thinking import resolve_escalation_target


def test_china_origin_blocked_on_customer_data_without_guard():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6", tenant_provider="anthropic",
        configured_model="z-ai/glm-5.2", configured_provider="openrouter",
        flag_enabled=True, allow_china_origin=False, is_customer_data=True,
    )
    # Blocked → fall back to the tenant's own model/provider
    assert target == ("claude-sonnet-4-6", "anthropic")


def test_china_origin_allowed_when_guard_and_flag_set():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6", tenant_provider="anthropic",
        configured_model="z-ai/glm-5.2", configured_provider="openrouter",
        flag_enabled=True, allow_china_origin=True, is_customer_data=True,
    )
    assert target == ("z-ai/glm-5.2", "openrouter")


def test_flag_off_uses_native_fallback():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6", tenant_provider="anthropic",
        configured_model="z-ai/glm-5.2", configured_provider="openrouter",
        flag_enabled=False, allow_china_origin=True, is_customer_data=True,
    )
    assert target == ("claude-sonnet-4-6", "anthropic")


def test_non_china_configured_model_allowed_without_china_guard():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6", tenant_provider="anthropic",
        configured_model="openai/gpt-4o-mini", configured_provider="openrouter",
        flag_enabled=True, allow_china_origin=False, is_customer_data=True,
    )
    assert target == ("openai/gpt-4o-mini", "openrouter")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_glm_thinking_guard.py -v`
Expected: FAIL — `cannot import name 'resolve_escalation_target'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/chat/thinking.py`:

```python
# Model id prefixes considered China-origin for residency purposes.
_CHINA_ORIGIN_PREFIXES = ("z-ai/", "glm-", "deepseek", "qwen", "moonshot", "kimi")


def _is_china_origin(model: str | None) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) or p in m for p in _CHINA_ORIGIN_PREFIXES)


def resolve_escalation_target(
    *,
    tenant_model: str,
    tenant_provider: str,
    configured_model: str,
    configured_provider: str,
    flag_enabled: bool,
    allow_china_origin: bool,
    is_customer_data: bool,
) -> tuple[str, str]:
    """Pick (model, provider) for an escalated turn.

    Returns the tenant's own model/provider (native fallback) UNLESS a thinking
    model is configured, the flag is on, and — for China-origin models on a
    customer-data turn — the hard residency guard is explicitly set.
    """
    native = (tenant_model, tenant_provider)
    if not flag_enabled or not configured_model:
        return native
    if is_customer_data and _is_china_origin(configured_model) and not allow_china_origin:
        return native  # PHYSICAL BLOCK
    return (configured_model, configured_provider)
```

> Implementer wiring (no behavior change while flag is off): in `base_agent.py`, when `escalate_reasoning` fires, in addition to `thinking.next_level(...)`, optionally switch the model/provider for the continuation by calling `resolve_escalation_target(...)` with `settings.CHAT_THINKING_MODEL/PROVIDER`, the `chat_glm_thinking` feature flag (`feature_flag_service.is_enabled`), `settings.ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA`, and `is_customer_data=True` (chat is always customer-data). With defaults (flag off), this always returns the native model — zero behavior change until explicitly unlocked. Thread the chosen model/provider into the adapter selection for subsequent steps (re-`get_adapter` if provider changed). Cover with one integration test mirroring `test_chat_multi_provider.py` that asserts: flag off ⇒ adapter/model unchanged after escalation.

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_glm_thinking_guard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/thinking.py backend/tests/test_glm_thinking_guard.py
git commit -m "feat(chat): GLM-tier escalation resolver with hard China-origin residency block [T2]"
```

---

## Final verification + T2 gates

- [ ] **Full suite (zero regressions):**

Run: `backend/.venv/bin/python -m pytest backend/tests/ -q`
Expected: PASS (no regressions). Also run `ruff check backend/ && ruff format --check backend/`.

- [ ] **Docker DB parity:** rebuild backend container so the new settings load: `docker compose up -d --build backend`. (No alembic migration in this plan — all changes are code/config.)

- [ ] **Benchmark gate (T2, mandatory for chat-path change):** Even on Sonnet, enabling thinking changes the chat path. Run the vs-Claude+MCP benchmark and confirm match-or-beat before merge (`memory/feedback_benchmark_vs_claude_mcp`). This guards Phase A independent of GLM.

- [ ] **Multi-angle review (T2, blocking, pre-merge):** `Workflow({name: "code-review-multiangle", args: {target: "<PR#>"}})`. Read `status` first (`INCOMPLETE`/`PREP_FAILED` ⇒ re-run; check `codex_used == true`). Resolve every CONFIRMED + PLAUSIBLE-major finding before merge.

- [ ] **Independent grill before PR-ready:** `/grill-me` (codex) over the diff — Claude-on-Claude review shares blind spots.

- [ ] **GLM unlock (OUT OF SCOPE here — do NOT flip in this PR):** flipping `chat_glm_thinking` + `ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA` on real traffic requires, separately: (1) residency-policy sign-off + `memory/` rule update, (2) GLM-5.2 passing the Claude+MCP benchmark, (3) tool-calling-under-reasoning validation on the agent loop. Tracked, not done here.

- [ ] **Push to BOTH repos** (`origin` + `framework`) and open the PR referencing ClickUp `86baku1yf`.

---

## Self-review (plan ↔ spec)

- **Spec §1.1 thinking levels** → A1. **§1.2 Anthropic mapping + block round-trip** → A3. **§1.3 Layer 1 self-regulation + simple-lookup exclusion** → A6. **§1.4 Layer 2 escalate_reasoning** → A4 (carrier) + A5 (tool/bump). **§1.5 `<reasoning>` reconcile** → A7. **§1.6 settings** → A2. **§2 OpenRouter adapter + registry** → B1 + B2. **§3 GLM tier flag + guard + BYOK** → C1 + C2. All spec sections map to a task.
- **Type consistency:** `thinking_level: str | None` and `LLMResponse.thinking_blocks: list[dict]` used identically across A3/A4/B1; `thinking.budget_for/reasoning_effort/next_level/resolve_escalation_target` signatures match their call sites; `compute_thinking_level` keyword-only args match its test.
- **No placeholders:** every code/test step contains real code; deferrable A7 is explicitly marked and the suite still passes without it (the strip-regex remains).
- **Note:** A4's `_RecordingAdapter` test asserts the threading invariant; implementer may lower the assertion to the helper that issues the adapter call if `UnifiedAgent.run_streaming` needs heavier mocking — the invariant (level reaches `stream_message`) is what matters.
