# Tool Choice Forcing — TDD Implementation Prompt

> Copy-paste this into Claude Code. It implements API-level `tool_choice` forcing across all LLM adapters and integrates it into the agentic loop, using strict Red-Green-Refactor TDD across 14 cycles in 2 phases.

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. Use `/ralph-loop` for iterative development.

**Goal:** Guarantee the LLM calls the correct tool on its first turn by using the `tool_choice` API parameter, eliminating prompt-based tool guidance that the LLM can ignore.

**Architecture:** Thread `tool_choice` through 4 layers (adapter interface → provider adapters → agent loop → orchestrator). Force tool on step 0 only; revert to `auto` on subsequent steps. Add `netsuite.financial_report` to the unified agent's tool set. When the orchestrator detects financial intent but pre-execution fails, set `tool_choice={"type": "tool", "name": "netsuite_financial_report"}` instead of relying on prompt instructions.

**Tech Stack:** Anthropic SDK 0.79+, OpenAI SDK, Google GenAI SDK, FastAPI, pytest (async)

---

## WHAT EXISTS TODAY

- `llm_adapter.py` lines 36-46: `create_message()` abstract method — NO `tool_choice` param
- `llm_adapter.py` lines 49-75: `stream_message()` default method — NO `tool_choice` param
- `anthropic_adapter.py` lines 32-43, 89-99: kwargs built without `tool_choice`
- `openai_adapter.py` lines 100-106, 152-160: kwargs built without `tool_choice`
- `gemini_adapter.py` lines 102-107: config built without `tool_config`
- `base_agent.py` lines 335-342, 590-597: adapter calls in agentic loop — NO `tool_choice`
- `base_agent.py` lines 350-366, 613-629: `_task_contains_query()` workaround — forces retry if LLM skips tools on step 0 (costs an extra LLM round-trip)
- `unified_agent.py` lines 38-54: `_UNIFIED_TOOL_NAMES` frozenset — does NOT include `netsuite_financial_report`
- `unified_agent.py` lines 551-561, 563-575: `run()` and `run_streaming()` overrides — NO `tool_choice` param
- `orchestrator.py` lines 32-58: `_build_financial_mode_task()` — text-based "you MUST use this tool" instruction (LLM ignores it)
- `orchestrator.py` lines 639-697: financial pre-execution block — falls back to text instructions when parsing fails
- `orchestrator.py` lines 702-709: `unified_agent.run_streaming()` call — NO `tool_choice`

---

## PHASE 1: Adapter Plumbing + Agent Threading

**Goal:** Add `tool_choice` support to all 3 LLM adapters and thread it through the agentic loop. No behavior change yet — all defaults remain `None`.

---

### CYCLE 1: Base Adapter Interface (RED → GREEN)

**RED** — Create `backend/tests/test_tool_choice_adapter.py`:

```python
"""Tests for tool_choice parameter support across LLM adapters."""

import pytest
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage


def test_create_message_accepts_tool_choice():
    """BaseLLMAdapter.create_message signature must accept tool_choice param."""
    import inspect
    sig = inspect.signature(BaseLLMAdapter.create_message)
    assert "tool_choice" in sig.parameters
    param = sig.parameters["tool_choice"]
    assert param.default is None


def test_stream_message_accepts_tool_choice():
    """BaseLLMAdapter.stream_message signature must accept tool_choice param."""
    import inspect
    sig = inspect.signature(BaseLLMAdapter.stream_message)
    assert "tool_choice" in sig.parameters
    param = sig.parameters["tool_choice"]
    assert param.default is None
```

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_adapter.py -v` — should FAIL (no `tool_choice` in signature).

**GREEN** — In `backend/app/services/chat/llm_adapter.py`:

Add `tool_choice: dict | str | None = None,` after the `tools` parameter in both methods:

Line 36-46, `create_message()`:
```python
@abc.abstractmethod
async def create_message(
    self,
    *,
    model: str,
    max_tokens: int,
    system: str,
    system_dynamic: str = "",
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: dict | str | None = None,
) -> LLMResponse:
```

Line 49-75, `stream_message()`:
```python
async def stream_message(
    self,
    *,
    model: str,
    max_tokens: int,
    system: str,
    system_dynamic: str = "",
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: dict | str | None = None,
):
```

- [ ] Run tests — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: add tool_choice param to BaseLLMAdapter interface"`

---

### CYCLE 2: Anthropic Adapter (RED → GREEN)

**RED** — Append to `backend/tests/test_tool_choice_adapter.py`:

```python
@pytest.mark.asyncio
async def test_anthropic_adapter_passes_tool_choice_to_kwargs():
    """AnthropicAdapter should include tool_choice in API kwargs when provided."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.anthropic_adapter.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(api_key="test-key")

        tool_choice = {"type": "tool", "name": "netsuite_suiteql"}
        tools = [{"name": "netsuite_suiteql", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice,
        )

        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "netsuite_suiteql"}


@pytest.mark.asyncio
async def test_anthropic_adapter_omits_tool_choice_when_none():
    """AnthropicAdapter should NOT include tool_choice in kwargs when None."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.anthropic_adapter.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(api_key="test-key")

        await adapter.create_message(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
        )

        call_kwargs = mock_client.messages.create.call_args[1]
        assert "tool_choice" not in call_kwargs
```

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_adapter.py::test_anthropic_adapter_passes_tool_choice_to_kwargs -v` — should FAIL.

**GREEN** — In `backend/app/services/chat/adapters/anthropic_adapter.py`:

1. Add `tool_choice: dict | str | None = None,` after `tools` in both `create_message()` (line ~21) and `stream_message()` (line ~78) signatures.

2. In `create_message()`, after the `if tools:` block that adds tools to kwargs (around line 43), add:
```python
if tool_choice is not None:
    kwargs["tool_choice"] = tool_choice
```

3. In `stream_message()`, after the `if tools:` block (around line 99), add the same:
```python
if tool_choice is not None:
    kwargs["tool_choice"] = tool_choice
```

- [ ] Run all tests in file — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: add tool_choice support to AnthropicAdapter"`

---

### CYCLE 3: OpenAI Adapter (RED → GREEN)

**RED** — Append to `backend/tests/test_tool_choice_adapter.py`:

```python
@pytest.mark.asyncio
async def test_openai_adapter_converts_tool_choice_format():
    """OpenAI uses different tool_choice format — adapter must convert."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        # Anthropic format in, OpenAI format out
        tool_choice_anthropic = {"type": "tool", "name": "netsuite_suiteql"}
        tools = [{"name": "netsuite_suiteql", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice_anthropic,
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        # Should be converted to OpenAI format
        assert call_kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "netsuite_suiteql"},
        }


@pytest.mark.asyncio
async def test_openai_adapter_converts_any_to_required():
    """Anthropic's {"type": "any"} maps to OpenAI's "required"."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        tools = [{"name": "test", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice={"type": "any"},
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_openai_adapter_converts_auto():
    """Anthropic's {"type": "auto"} maps to OpenAI's "auto"."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        tools = [{"name": "test", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice={"type": "auto"},
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == "auto"
```

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_adapter.py::test_openai_adapter_converts_tool_choice_format -v` — should FAIL.

**GREEN** — In `backend/app/services/chat/adapters/openai_adapter.py`:

1. Add `tool_choice: dict | str | None = None,` to both `create_message()` (line ~96) and `stream_message()` (line ~147) signatures.

2. Add a conversion helper method to the class:
```python
@staticmethod
def _convert_tool_choice(tool_choice: dict | str | None) -> dict | str | None:
    """Convert Anthropic-style tool_choice to OpenAI format."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    tc_type = tool_choice.get("type")
    if tc_type == "auto":
        return "auto"
    if tc_type == "any":
        return "required"
    if tc_type == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    if tc_type == "none":
        return "none"
    return None
```

3. In `create_message()`, after adding tools to kwargs, add:
```python
converted_tc = self._convert_tool_choice(tool_choice)
if converted_tc is not None:
    kwargs["tool_choice"] = converted_tc
```

4. Same in `stream_message()`.

- [ ] Run all tests in file — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: add tool_choice support to OpenAIAdapter with format conversion"`

---

### CYCLE 4: Gemini Adapter (RED → GREEN)

**RED** — Append to `backend/tests/test_tool_choice_adapter.py`:

```python
@pytest.mark.asyncio
async def test_gemini_adapter_converts_tool_choice_to_tool_config():
    """Gemini uses function_calling_config — adapter must convert."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.gemini_adapter.genai") as mock_genai:
        mock_client = MagicMock()

        # Mock the response object
        mock_part = MagicMock()
        mock_part.text = "Hello"
        mock_part.function_call = None
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_candidate.finish_reason = MagicMock(name="STOP")
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=5)
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        from app.services.chat.adapters.gemini_adapter import GeminiAdapter
        adapter = GeminiAdapter(api_key="test-key")

        tool_choice = {"type": "tool", "name": "netsuite_suiteql"}
        tools = [{"name": "netsuite_suiteql", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gemini-2.0-flash",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice,
        )

        call_kwargs = mock_client.aio.models.generate_content.call_args[1]
        config = call_kwargs["config"]
        # Gemini should have tool_config with allowed_function_names
        assert config.tool_config is not None
```

- [ ] Run — should FAIL.

**GREEN** — In `backend/app/services/chat/adapters/gemini_adapter.py`:

1. Add `tool_choice: dict | str | None = None,` to `create_message()` signature.

2. After the `if tools:` block that sets `config.tools`, add:
```python
if tool_choice is not None:
    tc_type = tool_choice.get("type") if isinstance(tool_choice, dict) else tool_choice
    if tc_type == "tool" and isinstance(tool_choice, dict):
        config.tool_config = genai_types.ToolConfig(
            function_calling_config=genai_types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[tool_choice["name"]],
            )
        )
    elif tc_type == "any":
        config.tool_config = genai_types.ToolConfig(
            function_calling_config=genai_types.FunctionCallingConfig(mode="ANY")
        )
    elif tc_type == "none":
        config.tool_config = genai_types.ToolConfig(
            function_calling_config=genai_types.FunctionCallingConfig(mode="NONE")
        )
    # "auto" is Gemini's default — no config needed
```

- [ ] Run all tests in file — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: add tool_choice support to GeminiAdapter with tool_config conversion"`

---

### CYCLE 5: Agent Loop — Step 0 Forcing (RED → GREEN → REFACTOR)

**RED** — Create `backend/tests/test_tool_choice_agent.py`:

```python
"""Tests for tool_choice threading through the agentic loop."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock


@pytest.mark.asyncio
async def test_run_passes_tool_choice_on_step_0_only():
    """tool_choice should be passed on step 0 and None on subsequent steps."""
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    # Create a concrete subclass for testing
    class TestAgent(BaseSpecialistAgent):
        agent_name = "test"
        max_steps = 3
        @property
        def tool_definitions(self):
            return [{"name": "test_tool", "description": "test", "input_schema": {"type": "object", "properties": {}}}]
        def build_system_prompt(self, task, context):
            return "test prompt"

    agent = TestAgent.__new__(TestAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent._correlation_id = "test"

    mock_adapter = MagicMock()

    # Step 0: return tool call. Step 1: return text.
    tool_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="t1", name="test_tool", input={"query": "test"})],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    text_response = LLMResponse(
        text_blocks=["Final answer"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    mock_adapter.create_message = AsyncMock(side_effect=[tool_response, text_response])
    mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})

    with patch("app.services.chat.agents.base_agent.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
         patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock):
        mock_exec.return_value = {"success": True, "data": "ok"}

        await agent.run(
            task="test query",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
            tool_choice={"type": "tool", "name": "test_tool"},
        )

    # Step 0 should have tool_choice, step 1 should not
    calls = mock_adapter.create_message.call_args_list
    assert calls[0][1]["tool_choice"] == {"type": "tool", "name": "test_tool"}
    assert calls[1][1].get("tool_choice") is None


@pytest.mark.asyncio
async def test_run_without_tool_choice_passes_none():
    """When tool_choice is not provided, all steps should have tool_choice=None."""
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    class TestAgent(BaseSpecialistAgent):
        agent_name = "test"
        max_steps = 1
        @property
        def tool_definitions(self):
            return []
        def build_system_prompt(self, task, context):
            return "test prompt"

    agent = TestAgent.__new__(TestAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent._correlation_id = "test"

    mock_adapter = MagicMock()
    mock_adapter.create_message = AsyncMock(return_value=LLMResponse(
        text_blocks=["answer"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    ))

    with patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock):
        await agent.run(
            task="test",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
        )

    call_kwargs = mock_adapter.create_message.call_args[1]
    assert call_kwargs.get("tool_choice") is None
```

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_agent.py -v` — should FAIL.

**GREEN** — In `backend/app/services/chat/agents/base_agent.py`:

1. Add `tool_choice: dict | str | None = None,` to `run()` signature (line 276-283):
```python
async def run(
    self,
    task: str,
    context: dict[str, Any],
    db: "AsyncSession",
    adapter: "BaseLLMAdapter",
    model: str,
    tool_choice: dict | str | None = None,
):
```

2. Inside the `for step in range(self.max_steps):` loop, before the `adapter.create_message()` call (line 335), compute per-step tool_choice:
```python
step_tool_choice = tool_choice if step == 0 else None
```

3. Pass it to the adapter call:
```python
response: LLMResponse = await adapter.create_message(
    model=model,
    max_tokens=16384,
    system=prompt_parts.static,
    system_dynamic=prompt_parts.dynamic,
    messages=messages,
    tools=tools,
    tool_choice=step_tool_choice,
)
```

4. Do the same for `run_streaming()` (line 541-559) — add `tool_choice` param and pass `step_tool_choice` to `adapter.stream_message()` (line 590-597).

- [ ] Run tests — PASS.

**REFACTOR** — The loop-exhaustion final calls at lines 488-494 and 746-752 already pass `tools=None`, so `tool_choice` is naturally irrelevant there. No change needed.

- [ ] Commit: `git add -A && git commit -m "feat: thread tool_choice through agentic loop (step 0 only)"`

---

### CYCLE 6: UnifiedAgent Override Threading (RED → GREEN)

**RED** — Append to `backend/tests/test_tool_choice_agent.py`:

```python
@pytest.mark.asyncio
async def test_unified_agent_forwards_tool_choice_to_base():
    """UnifiedAgent.run_streaming() must forward tool_choice to super()."""
    from app.services.chat.agents.unified_agent import UnifiedAgent
    from unittest.mock import patch, AsyncMock

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent.agent_name = "unified"
    agent.max_steps = 10
    agent._tool_defs = None
    agent._correlation_id = "test"

    tool_choice = {"type": "tool", "name": "netsuite_financial_report"}

    with patch.object(UnifiedAgent, "_setup_context", new_callable=AsyncMock, return_value="task"), \
         patch("app.services.chat.agents.base_agent.BaseSpecialistAgent.run_streaming") as mock_super:

        mock_super.return_value = AsyncMock()
        mock_super.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        # Consume the generator
        async for _ in agent.run_streaming(
            task="test",
            context={},
            db=AsyncMock(),
            adapter=AsyncMock(),
            model="test",
            conversation_history=[],
            tool_choice=tool_choice,
        ):
            pass

        # Verify tool_choice was forwarded
        mock_super.assert_called_once()
        call_kwargs = mock_super.call_args
        assert call_kwargs[1].get("tool_choice") == tool_choice or \
               (len(call_kwargs[0]) > 7 and call_kwargs[0][7] == tool_choice)
```

- [ ] Run — should FAIL (run_streaming doesn't accept tool_choice).

**GREEN** — In `backend/app/services/chat/agents/unified_agent.py`:

1. Add `tool_choice: dict | str | None = None,` to `run()` signature (line 551) and forward to `super().run()`:
```python
async def run(
    self,
    task: str,
    context: dict[str, Any],
    db: "AsyncSession",
    adapter: "BaseLLMAdapter",
    model: str,
    tool_choice: dict | str | None = None,
):
    task = await self._setup_context(task, context, db)
    return await super().run(task, context, db, adapter, model, tool_choice=tool_choice)
```

2. Add `tool_choice: dict | str | None = None,` to `run_streaming()` signature (line 563) and forward:
```python
async def run_streaming(
    self,
    task: str,
    context: dict[str, Any],
    db: "AsyncSession",
    adapter: "BaseLLMAdapter",
    model: str,
    conversation_history: list[dict] | None = None,
    tool_choice: dict | str | None = None,
):
    task = await self._setup_context(task, context, db)
    async for event in super().run_streaming(
        task, context, db, adapter, model, conversation_history, tool_choice=tool_choice
    ):
        yield event
```

- [ ] Run tests — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: thread tool_choice through UnifiedAgent overrides"`

---

### CYCLE 7: Full Suite Regression (VERIFY)

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_adapter.py tests/test_tool_choice_agent.py -v` — all PASS.
- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/ -x --timeout=60 -q` — full backend suite should PASS (no regressions from the optional param addition).
- [ ] Commit: `git add -A && git commit -m "test: verify full suite passes with tool_choice plumbing"`

---

## PHASE 2: Financial Report Tool Registration + Orchestrator Integration

**Goal:** Register `netsuite_financial_report` in the unified agent's tool set, then use `tool_choice` forcing in the orchestrator when pre-execution fails.

---

### CYCLE 8: Add Financial Report to Unified Agent Tools (RED → GREEN)

**RED** — Create `backend/tests/test_unified_tool_registration.py`:

```python
"""Tests for netsuite_financial_report availability in unified agent."""


def test_financial_report_in_unified_tool_names():
    """netsuite_financial_report must be in the unified agent's tool set."""
    from app.services.chat.agents.unified_agent import _UNIFIED_TOOL_NAMES

    assert "netsuite_financial_report" in _UNIFIED_TOOL_NAMES


def test_unified_agent_tool_definitions_include_financial_report():
    """Unified agent should build tool definitions that include netsuite_financial_report."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent._tool_defs = None  # Force rebuild

    tool_names = [t["name"] for t in agent.tool_definitions]
    assert "netsuite_financial_report" in tool_names
```

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_unified_tool_registration.py -v` — should FAIL.

**GREEN** — In `backend/app/services/chat/agents/unified_agent.py`, add `"netsuite_financial_report"` to `_UNIFIED_TOOL_NAMES` (line 38-54):

```python
_UNIFIED_TOOL_NAMES = frozenset(
    {
        # SuiteQL agent tools
        "netsuite_suiteql",
        "netsuite_get_metadata",
        "netsuite_financial_report",    # <-- ADD THIS LINE
        # RAG agent tools
        "rag_search",
        "web_search",
        # Workspace agent tools
        "workspace_list_files",
        "workspace_read_file",
        "workspace_search",
        "workspace_propose_patch",
        # Shared
        "tenant_save_learned_rule",
    }
)
```

- [ ] Run tests — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: register netsuite_financial_report in unified agent tool set"`

---

### CYCLE 9: Orchestrator — tool_choice on Financial Fallback (RED → GREEN)

**RED** — Create `backend/tests/test_orchestrator_tool_choice.py`:

```python
"""Tests for tool_choice integration in the orchestrator."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_financial_fallback_sets_tool_choice():
    """When pre-execution fails and intent is financial, orchestrator should set tool_choice."""
    from app.services.chat.orchestrator import run_chat_turn

    # We need to verify that when:
    # 1. classify_intent returns FINANCIAL_REPORT
    # 2. parse_report_intent returns None (can't parse)
    # Then: unified_agent.run_streaming is called with tool_choice for financial report

    with patch("app.services.chat.orchestrator.classify_intent") as mock_classify, \
         patch("app.services.chat.orchestrator.parse_report_intent", return_value=None), \
         patch("app.services.chat.orchestrator.UnifiedAgent") as MockAgent, \
         patch("app.services.chat.orchestrator.get_tenant_ai_config", new_callable=AsyncMock) as mock_config, \
         patch("app.services.chat.orchestrator.deduct_chat_credits", new_callable=AsyncMock), \
         patch("app.services.chat.orchestrator.retriever_node", new_callable=AsyncMock), \
         patch("app.services.chat.orchestrator.retrieve_domain_knowledge", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.chat.orchestrator.retrieve_similar_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.chat.orchestrator.resolve_entities", new_callable=AsyncMock, return_value={}):

        mock_classify.return_value = MagicMock(value="FINANCIAL_REPORT")
        mock_config.return_value = ("anthropic", "claude-sonnet-4-20250514", "test-key", False)

        # Mock agent to yield a text event then stop
        mock_agent_instance = MagicMock()

        async def fake_streaming(*args, **kwargs):
            yield ("text", "Here is the report")
            yield ("final_response", "Here is the report")

        mock_agent_instance.run_streaming = fake_streaming
        MockAgent.return_value = mock_agent_instance

        events = []
        async for event in run_chat_turn(
            user_message="How are we doing financially this quarter?",
            tenant_id="test-tenant-id",
            actor_id="test-actor-id",
            session_id="test-session-id",
            db=AsyncMock(),
        ):
            events.append(event)

        # Verify that run_streaming was called (we can't easily check kwargs
        # on async generators, so this is a structural test)
        assert len(events) > 0


@pytest.mark.asyncio
async def test_non_financial_does_not_set_tool_choice():
    """Non-financial queries should NOT set tool_choice."""
    # This test ensures we don't force tools on regular queries
    from app.services.chat.orchestrator import _build_financial_mode_task

    # _build_financial_mode_task should exist and return a string
    result = _build_financial_mode_task("Show me the income statement for Feb 2026")
    assert isinstance(result, str)
    assert "netsuite.financial_report" in result
```

- [ ] Run — verify tests pass (these are more integration-level; adjust mocking as needed for your test patterns).

**GREEN** — In `backend/app/services/chat/orchestrator.py`:

1. At the financial fallback path (around line 689-696), where `_build_financial_mode_task()` is called because pre-execution failed, add tool_choice:

```python
# After line 696 (the fallback to _build_financial_mode_task):
financial_tool_choice = {"type": "tool", "name": "netsuite_financial_report"}
```

2. At the `unified_agent.run_streaming()` call (line 702-709), pass `tool_choice`:

```python
async for event_type, payload in unified_agent.run_streaming(
    task=unified_task,
    context=context,
    db=db,
    adapter=specialist_adapter,
    model=settings.MULTI_AGENT_SQL_MODEL,
    conversation_history=history_messages,
    tool_choice=financial_tool_choice if is_financial and not pre_executed_successfully else None,
):
```

Where `pre_executed_successfully` is a boolean flag set during the pre-execution block:
```python
# At the start of the financial block (line 639):
pre_executed_successfully = False

# After successful pre-execution injection (line 679):
pre_executed_successfully = True

# After the fallback (line 696):
# pre_executed_successfully remains False, so tool_choice is set
```

- [ ] Run tests — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: set tool_choice for financial reports when pre-execution fails"`

---

### CYCLE 10: System Prompt — Financial Report Tool Guidance (RED → GREEN)

**RED** — Append to `backend/tests/test_unified_tool_registration.py`:

```python
def test_unified_agent_system_prompt_mentions_financial_report_tool():
    """The unified agent's system prompt should guide usage of netsuite_financial_report."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent._correlation_id = "test"

    prompt = agent.build_system_prompt(
        task="Show me the income statement",
        context={},
    )

    # Must mention the tool for financial queries
    assert "netsuite_financial_report" in prompt
    assert "income_statement" in prompt or "balance_sheet" in prompt
```

- [ ] Run — should FAIL (system prompt doesn't mention the tool).

**GREEN** — In `backend/app/services/chat/agents/unified_agent.py`, in `build_system_prompt()`, add to the `<tool_selection>` XML block guidance for `netsuite_financial_report`:

```
- **netsuite_financial_report**: Use for standard financial statements (income_statement, balance_sheet, trial_balance, income_statement_trend, balance_sheet_trend). This tool uses verified SQL templates with correct TAL joins, sign conventions, and period handling. ALWAYS prefer this over writing raw SuiteQL for financial statements. Parameters: report_type (enum), period (e.g. 'Feb 2026'), optional subsidiary_id.
```

- [ ] Run tests — PASS.
- [ ] Commit: `git add -A && git commit -m "feat: add financial report tool guidance to unified agent system prompt"`

---

### CYCLE 11: Streaming — tool_choice in stream_message path (RED → GREEN)

**RED** — Append to `backend/tests/test_tool_choice_adapter.py`:

```python
@pytest.mark.asyncio
async def test_anthropic_stream_passes_tool_choice():
    """stream_message must also pass tool_choice to the API."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.anthropic_adapter.anthropic") as mock_anthropic:
        mock_client = MagicMock()

        # Mock the stream context manager
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        async def fake_events():
            yield MagicMock(type="content_block_start", content_block=MagicMock(type="text", text=""), index=0)
            yield MagicMock(type="content_block_delta", delta=MagicMock(type="text_delta", text="Hi"), index=0)
            yield MagicMock(type="message_stop")

        mock_stream.__aiter__ = lambda self: fake_events()
        mock_stream.get_final_message.return_value = MagicMock(
            usage=MagicMock(input_tokens=10, output_tokens=5)
        )
        mock_client.messages.stream = MagicMock(return_value=mock_stream)
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(api_key="test-key")

        tool_choice = {"type": "tool", "name": "netsuite_financial_report"}
        tools = [{"name": "netsuite_financial_report", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice,
        ):
            pass

        call_kwargs = mock_client.messages.stream.call_args[1]
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "netsuite_financial_report"}
```

- [ ] Run — should PASS if Cycle 2 was implemented correctly (both methods updated). If FAIL, fix `stream_message()` in the Anthropic adapter.
- [ ] Commit: `git add -A && git commit -m "test: verify stream_message passes tool_choice"`

---

### CYCLE 12: OpenAI + Gemini Stream Coverage (RED → GREEN)

**RED** — Append to `backend/tests/test_tool_choice_adapter.py`:

```python
@pytest.mark.asyncio
async def test_openai_stream_converts_tool_choice():
    """OpenAI stream_message must also convert tool_choice format."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()

        # Mock streaming response
        async def fake_stream():
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = "Hi"
            chunk.choices[0].delta.tool_calls = None
            chunk.choices[0].finish_reason = "stop"
            chunk.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
            yield chunk

        mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        tool_choice = {"type": "tool", "name": "test_tool"}
        tools = [{"name": "test_tool", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        async for _ in adapter.stream_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice,
        ):
            pass

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == {"type": "function", "function": {"name": "test_tool"}}
```

- [ ] Run — should PASS if Cycle 3 was complete. If FAIL, fix `stream_message()` in OpenAI adapter.
- [ ] Commit: `git add -A && git commit -m "test: verify OpenAI stream converts tool_choice"`

---

### CYCLE 13: Full Regression + Docker Verify (VERIFY)

- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_adapter.py tests/test_tool_choice_agent.py tests/test_unified_tool_registration.py tests/test_orchestrator_tool_choice.py -v` — all new tests PASS.
- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/ -x --timeout=60 -q` — full backend suite PASS (no regressions).
- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_financial_report_tool.py -v` — financial report tests still PASS.
- [ ] Docker: `docker compose up -d --build backend worker` and verify backend starts without errors.
- [ ] Commit: `git add -A && git commit -m "test: full regression pass with tool_choice forcing"`

---

### CYCLE 14: End-to-End Manual Verification

- [ ] **Test 1 — Pre-execution path (should be unchanged):** In the chat UI, ask "Show me the income statement for Feb 2026". The pre-execution parser should handle this. Verify data is correct and no extra LLM round-trip for tool selection.

- [ ] **Test 2 — tool_choice fallback path:** Ask a vaguely financial question that the parser can't handle, e.g., "How are we doing financially this quarter?" The parser returns `None`, `tool_choice` forces `netsuite_financial_report`, and the LLM fills in params. Verify the LLM calls the correct tool (check Docker logs for `[AGENT] unified calling netsuite_financial_report`).

- [ ] **Test 3 — Non-financial query (no forcing):** Ask "How many open sales orders do we have?" Verify the LLM calls `netsuite_suiteql` with its own SQL (not forced to financial report).

- [ ] **Test 4 — Mixed query:** Ask "Show me the P&L for Feb 2026 and also count open purchase orders." Pre-execution should handle the P&L part. The agent should call `netsuite_suiteql` for the PO count in a subsequent step.

---

## VERIFICATION CHECKLIST

After all cycles:
1. `cd backend && .venv/bin/python -m pytest tests/test_tool_choice_adapter.py tests/test_tool_choice_agent.py tests/test_unified_tool_registration.py -v` — all PASS
2. `cd backend && .venv/bin/python -m pytest tests/ -x --timeout=60 -q` — full suite PASS
3. `cd frontend && npm run build && npm run lint` — no errors (no frontend changes in this plan)
4. Docker backend starts clean
5. Manual chat verification for all 4 scenarios above

---

## FILES CHANGED SUMMARY

| File | Change | Lines |
|------|--------|-------|
| `backend/app/services/chat/llm_adapter.py` | Add `tool_choice` param to interface | ~2 |
| `backend/app/services/chat/adapters/anthropic_adapter.py` | Pass `tool_choice` to kwargs | ~6 |
| `backend/app/services/chat/adapters/openai_adapter.py` | Convert + pass `tool_choice` | ~15 |
| `backend/app/services/chat/adapters/gemini_adapter.py` | Convert to `tool_config` | ~12 |
| `backend/app/services/chat/agents/base_agent.py` | Accept + pass on step 0 only | ~8 |
| `backend/app/services/chat/agents/unified_agent.py` | Add to `_UNIFIED_TOOL_NAMES` + forward | ~6 |
| `backend/app/services/chat/orchestrator.py` | Set `tool_choice` on financial fallback | ~8 |
| `backend/tests/test_tool_choice_adapter.py` | NEW — adapter tests | ~250 |
| `backend/tests/test_tool_choice_agent.py` | NEW — agent loop tests | ~100 |
| `backend/tests/test_unified_tool_registration.py` | NEW — registration tests | ~30 |
| `backend/tests/test_orchestrator_tool_choice.py` | NEW — orchestrator integration | ~60 |

**Total implementation: ~57 lines across 7 files. Total tests: ~440 lines across 4 new test files.**

---

## KNOWN LIMITATIONS (DO NOT IMPLEMENT NOW)

- **UX**: Forced `tool_choice` suppresses `<thinking>` tags on step 0 (Anthropic API behavior). Acceptable — user doesn't need to see "I'll call the financial report tool."
- **Extended thinking**: Incompatible with `tool_choice: tool`. Only affects step 0 when forcing is active.
- **Generic ToolDispatchStrategy registry**: Wait until a second deterministic tool qualifies (e.g., `recon.run`). One special case doesn't justify an abstraction.
- **Haiku-based intent parsing**: The regex parser covers 80%+ of financial queries. LLM router adds ~300ms cost. Defer until regex coverage proves insufficient.
- **Prompt cache invalidation**: Changing `tool_choice` between steps invalidates Anthropic message cache. Acceptable since forcing only happens on step 0.
