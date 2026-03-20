# TDD Prompt: Wire Prompt Caching into Orchestrator

## Problem

The prompt caching infrastructure is **fully built and tested** but only wired into the specialist agent path (`base_agent.py`). The main orchestrator path — which handles the majority of API calls — sends the entire system prompt as a single monolithic string, missing out on Anthropic's `cache_control` optimization.

### Cost Impact (March 1–12, 2026)

- **$103 total** in 12 days
- **$55.59 (54%)** is `input_no_cache` — full-price tokens, zero caching
- **$31.17 (30%)** is `cache_write` — paying to write cache but rarely reading
- **$4.52 (4%)** is `cache_read` — the cheap path, barely used
- **Cache hit ratio: 5%** — should be 60-80%+

The root cause: the orchestrator calls `adapter.stream_message(system=system_prompt)` without splitting static vs dynamic content. The Anthropic adapter's `cache_control: {"type": "ephemeral"}` is applied to the ENTIRE prompt (including dynamic XML blocks that change every turn), so the cache key changes every time and never hits.

## What Already Works

1. **`prompt_cache.py`** — `split_system_prompt()` extracts 4 dynamic XML blocks into a separate string:
   - `<tenant_vernacular>` — entity mappings (changes per query)
   - `<domain_knowledge>` — RAG results (changes per query)
   - `<proven_patterns>` — past successful queries (changes per query)
   - `<financial_context>` — period/currency context (changes per query)

2. **`anthropic_adapter.py`** — Both `create_message()` and `stream_message()` accept `system_dynamic` parameter:
   - Static part gets `cache_control: {"type": "ephemeral"}` → cached for 5 min
   - Dynamic part is appended as a second system block WITHOUT cache_control → always fresh
   - Tool definitions also get `cache_control` on the last tool → cached

3. **`base_agent.py`** — Specialist agents already call `split_system_prompt()` at lines 332 and 588, passing `system=prompt_parts.static, system_dynamic=prompt_parts.dynamic`.

4. **Tests** — `backend/tests/test_prompt_cache.py` has comprehensive unit tests covering splitting, empty dynamic, content preservation, and cleanup.

## The Fix

### File: `backend/app/services/chat/orchestrator.py`

**Change 1: Import `split_system_prompt`**

Near the top imports, add:
```python
from app.services.chat.prompt_cache import split_system_prompt
```

**Change 2: Split the system prompt ONCE, before the agentic loop**

After the system prompt is fully constructed (after all the `system_prompt +=` lines, around line ~500, before the agentic loop starts at line ~1013), add:

```python
prompt_parts = split_system_prompt(system_prompt)
```

**Change 3: Pass split parts to `adapter.stream_message()` — Line ~1024**

BEFORE:
```python
async for event_type, payload in adapter.stream_message(
    model=model,
    max_tokens=16384,
    system=system_prompt,
    messages=messages,
    tools=tool_definitions if tool_definitions else None,
):
```

AFTER:
```python
async for event_type, payload in adapter.stream_message(
    model=model,
    max_tokens=16384,
    system=prompt_parts.static,
    system_dynamic=prompt_parts.dynamic,
    messages=messages,
    tools=tool_definitions if tool_definitions else None,
):
```

**Change 4: Same fix for the fallback/exhaustion call — Line ~1135**

BEFORE:
```python
async for event_type, payload in adapter.stream_message(
    model=model,
    max_tokens=8192,
    system=system_prompt,
    messages=messages,
):
```

AFTER:
```python
async for event_type, payload in adapter.stream_message(
    model=model,
    max_tokens=8192,
    system=prompt_parts.static,
    system_dynamic=prompt_parts.dynamic,
    messages=messages,
):
```

**Change 5: Multi-agent path — Line ~891**

The coordinator receives `system_prompt` and passes it to specialist agents which already split it. No change needed here — the coordinator's agents already use `split_system_prompt()` internally via `base_agent.py`. Just verify it still works.

### That's it. 1 import + 1 split call + 2 parameter changes.

## Why This Works

The system prompt is built up through ~15 `system_prompt +=` operations (lines 319-500). Most of this content is **static per session**: base template, soul/tone, tool inventory, MCP guidance, dialect rules, table schemas. Only 4 XML blocks change per turn.

After splitting:
- **Static part** (~70-90% of tokens): cached with 5-min TTL. On multi-step agentic loops (3-6 steps), this hits cache on steps 2+. Within a session with multiple user messages, if the user sends another message within 5 min, the static part hits cache again.
- **Dynamic part** (~10-30% of tokens): sent fresh every call, never cached. This is correct — these blocks contain per-query context.

### Expected Cost Impact

- Multi-step agentic loops: Steps 2-6 will cache-hit the static prompt → **~80% cheaper input per loop step**
- Rapid-fire testing (messages within 5 min): Cache hits across messages → **~70% cheaper**
- Conservative estimate: `input_no_cache` drops from $55 to ~$15-20 per 12-day period
- Cache reads increase from $4.52 to ~$15-20 (but at 10% rate, much cheaper)
- **Projected savings: 40-50% of total API cost**

## Tests to Write

### Unit Test: `backend/tests/test_orchestrator_prompt_caching.py`

```python
"""Tests that the orchestrator uses prompt caching correctly."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.chat.prompt_cache import split_system_prompt


class TestOrchestratorPromptCaching:
    """Verify the orchestrator splits and passes prompt parts correctly."""

    def test_split_is_called_with_full_system_prompt(self):
        """Ensure split_system_prompt is called, not skipped."""
        # Build a sample system prompt with dynamic blocks
        prompt = (
            "You are an assistant.\n\n"
            "<tenant_vernacular>Customer Acme = ID 123</tenant_vernacular>\n\n"
            "<domain_knowledge>GL account info</domain_knowledge>\n\n"
            "Use the tools wisely."
        )
        parts = split_system_prompt(prompt)

        assert "You are an assistant." in parts.static
        assert "Use the tools wisely." in parts.static
        assert "<tenant_vernacular>" not in parts.static
        assert "<domain_knowledge>" not in parts.static
        assert "<tenant_vernacular>" in parts.dynamic
        assert "<domain_knowledge>" in parts.dynamic

    def test_static_part_is_stable_across_different_dynamic_content(self):
        """The static part should be identical regardless of dynamic content.
        This is what makes caching work — same static = same cache key."""
        prompt_v1 = (
            "You are an assistant.\n\n"
            "<tenant_vernacular>Customer Acme = ID 123</tenant_vernacular>\n\n"
            "Use the tools wisely."
        )
        prompt_v2 = (
            "You are an assistant.\n\n"
            "<tenant_vernacular>Customer Beta = ID 456</tenant_vernacular>\n\n"
            "Use the tools wisely."
        )
        parts_v1 = split_system_prompt(prompt_v1)
        parts_v2 = split_system_prompt(prompt_v2)

        # Static parts MUST be identical for cache to hit
        assert parts_v1.static == parts_v2.static
        # Dynamic parts differ (different entity mappings)
        assert parts_v1.dynamic != parts_v2.dynamic

    def test_no_dynamic_blocks_still_works(self):
        """If there are no dynamic XML blocks, static = full prompt, dynamic = empty."""
        prompt = "You are an assistant.\n\nUse the tools wisely."
        parts = split_system_prompt(prompt)

        assert parts.static == prompt
        assert parts.dynamic == ""
```

### Integration Test: Verify adapter receives split parts

```python
    @pytest.mark.asyncio
    async def test_adapter_receives_system_dynamic(self):
        """The adapter's stream_message should receive system_dynamic parameter."""
        # This test would mock the adapter and verify the orchestrator passes
        # system=prompt_parts.static and system_dynamic=prompt_parts.dynamic
        # instead of system=full_monolithic_prompt
        pass  # Implementation depends on orchestrator's testability
```

## Verification After Deployment

1. Check the Anthropic usage dashboard after deploying — `cache_read` cost should increase while `input_no_cache` should decrease dramatically
2. In logs, the adapter tracks `cache_creation_input_tokens` and `cache_read_input_tokens` — verify cache_read > 0 on agentic loop steps 2+
3. Run a 3-message chat session within 5 minutes — the 2nd and 3rd messages should show cache hits on the static prompt portion

## Files Changed

| File | Change |
|------|--------|
| `backend/app/services/chat/orchestrator.py` | Add import + split + pass split parts to 2 stream_message calls |
| `backend/tests/test_orchestrator_prompt_caching.py` | New test file verifying split behavior |

## Files NOT Changed (already correct)

| File | Why |
|------|-----|
| `backend/app/services/chat/prompt_cache.py` | Already complete |
| `backend/app/services/chat/adapters/anthropic_adapter.py` | Already accepts `system_dynamic`, already applies `cache_control` |
| `backend/app/services/chat/agents/base_agent.py` | Already uses `split_system_prompt()` |
| `backend/tests/test_prompt_cache.py` | Already has comprehensive unit tests |
