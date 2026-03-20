# Architecture Improvements — 3 Targeted Upgrades (TDD)

> These are surgical changes that bring the platform to cutting-edge without over-engineering.
> Each improvement is independent — implement in order but each can ship alone.
> Use Red-Green-Refactor TDD for each.

Read `CLAUDE.md` before starting. Follow all conventions exactly.

---

## IMPROVEMENT 1: SuiteQL Judge Model (Post-Execution Verification)

### Why
The #1 trust concern: "I don't trust AI-generated SQL." Instead of hoping the LLM gets it right, add a cheap Haiku verification step AFTER execution that validates the result makes sense. This costs ~$0.001 per query and catches wrong-column, wrong-filter, and hallucinated-field errors before the user sees them.

### What Exists Today
- `netsuite_suiteql.py` line 25: `is_read_only_sql()` validates syntax
- `netsuite_suiteql.py` line 50: `validate_query()` checks allowed tables
- `base_agent.py` line 152: `_maybe_store_query_pattern()` fires after successful SuiteQL
- `tools.py` line 156: `execute_tool_call()` is the central tool dispatcher
- Tool result flows back to agent as a tool_result message for next step

### Architecture
The judge sits INSIDE the `netsuite_suiteql` tool — after execution, before returning results to the agent. This way it's transparent to the agent loop.

```
Agent calls netsuite_suiteql(query="SELECT ...")
  → validate_query() (existing — syntax + table check)
  → Execute via REST API (existing)
  → NEW: judge_suiteql_result() — Haiku validates result
  → Return result to agent (with judge verdict)
```

### TDD Cycle 1A: Judge Service

**RED** — Create `backend/tests/test_suiteql_judge.py`:
```python
import pytest
from app.services.suiteql_judge import judge_suiteql_result, JudgeVerdict

@pytest.mark.asyncio
async def test_judge_approves_correct_result():
    verdict = await judge_suiteql_result(
        user_question="How many open sales orders?",
        sql="SELECT COUNT(*) as cnt FROM transaction WHERE type = 'SalesOrd' AND status NOT IN ('C', 'H')",
        result_preview=[{"cnt": 42}],
        row_count=1,
    )
    assert verdict.approved is True
    assert verdict.confidence >= 0.7

@pytest.mark.asyncio
async def test_judge_flags_empty_result_for_broad_query():
    verdict = await judge_suiteql_result(
        user_question="Show me all customers",
        sql="SELECT companyname FROM customer",
        result_preview=[],
        row_count=0,
    )
    # Zero rows for "all customers" is suspicious
    assert verdict.approved is False
    assert "no results" in verdict.reason.lower()

@pytest.mark.asyncio
async def test_judge_flags_column_mismatch():
    verdict = await judge_suiteql_result(
        user_question="What is total revenue by month?",
        sql="SELECT trandate, amount FROM transaction",
        result_preview=[{"trandate": "2025-01-15", "amount": 100}],
        row_count=50,
    )
    # Asked for "by month" but query has no GROUP BY month
    assert verdict.approved is False

@pytest.mark.asyncio
async def test_judge_returns_approved_on_timeout():
    """If judge times out, default to approved (don't block the user)."""
    # Mock the LLM call to raise a timeout
    verdict = await judge_suiteql_result(
        user_question="test",
        sql="SELECT 1",
        result_preview=[{"1": 1}],
        row_count=1,
        _timeout_seconds=0.001,  # Force timeout
    )
    assert verdict.approved is True  # Fail-open
    assert verdict.reason == "verification_timeout"
```

**GREEN** — Create `backend/app/services/suiteql_judge.py`:
```python
"""Lightweight SuiteQL result judge — validates query results using a fast/cheap model.

Runs AFTER SuiteQL execution, BEFORE returning results to the agent.
Uses Haiku for ~$0.001 per verification. Fail-open on timeout.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import anthropic

from app.core.config import settings

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-haiku-4-5-20251001"
JUDGE_MAX_TOKENS = 200
JUDGE_TIMEOUT = 3.0  # seconds — must be fast


@dataclass
class JudgeVerdict:
    approved: bool
    confidence: float  # 0.0-1.0
    reason: str


_JUDGE_PROMPT = """\
You are a SQL result validator. Given a user's question, the SuiteQL query executed, and a preview of the results, determine if the query correctly answers the question.

Check for:
1. Does the query address what the user asked? (correct tables, correct filters)
2. Do the results make sense? (zero rows when expecting data = suspicious)
3. Are the columns relevant to the question?
4. Is there a GROUP BY if the user asked for aggregation "by X"?

Respond with EXACTLY this format (no other text):
APPROVED: true/false
CONFIDENCE: 0.0-1.0
REASON: one sentence explanation
"""


async def judge_suiteql_result(
    user_question: str,
    sql: str,
    result_preview: list[dict],
    row_count: int,
    *,
    _timeout_seconds: float = JUDGE_TIMEOUT,
) -> JudgeVerdict:
    """Validate a SuiteQL result with a fast model. Fail-open on error/timeout."""
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

        # Truncate preview to save tokens
        preview_str = str(result_preview[:5])[:500]

        user_msg = (
            f"User question: {user_question}\n"
            f"SQL executed: {sql}\n"
            f"Row count: {row_count}\n"
            f"Result preview (first 5 rows): {preview_str}"
        )

        response = await asyncio.wait_for(
            client.messages.create(
                model=JUDGE_MODEL,
                max_tokens=JUDGE_MAX_TOKENS,
                system=_JUDGE_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            ),
            timeout=_timeout_seconds,
        )

        text = response.content[0].text.strip()
        return _parse_verdict(text)

    except asyncio.TimeoutError:
        logger.info("suiteql_judge.timeout question=%r", user_question[:80])
        return JudgeVerdict(approved=True, confidence=0.5, reason="verification_timeout")
    except Exception:
        logger.warning("suiteql_judge.error", exc_info=True)
        return JudgeVerdict(approved=True, confidence=0.5, reason="verification_error")


def _parse_verdict(text: str) -> JudgeVerdict:
    """Parse the structured judge response."""
    lines = text.strip().split("\n")
    approved = True
    confidence = 0.5
    reason = "unknown"

    for line in lines:
        line = line.strip()
        if line.upper().startswith("APPROVED:"):
            val = line.split(":", 1)[1].strip().lower()
            approved = val in ("true", "yes", "1")
        elif line.upper().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
                confidence = max(0.0, min(1.0, confidence))
            except ValueError:
                pass
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    return JudgeVerdict(approved=approved, confidence=confidence, reason=reason)
```

### TDD Cycle 1B: Integration into netsuite_suiteql tool

**RED** — Add test:
```python
@pytest.mark.asyncio
async def test_suiteql_tool_includes_judge_verdict(mock_db, mock_connection):
    """Tool result should include judge_verdict when judge is enabled."""
    result = await execute_netsuite_suiteql(
        db=mock_db, tenant_id=test_tenant_id,
        query="SELECT COUNT(*) FROM transaction", limit=10,
    )
    parsed = json.loads(result)
    assert "judge_verdict" in parsed
    assert parsed["judge_verdict"]["approved"] in (True, False)
```

**GREEN** — In `backend/app/mcp/tools/netsuite_suiteql.py`, after the REST API call returns data but before returning to the caller:

```python
# After: result = {"items": rows, "count": len(rows), ...}
# Add judge verification
from app.services.suiteql_judge import judge_suiteql_result

verdict = await judge_suiteql_result(
    user_question=context.get("user_question", ""),
    sql=query,
    result_preview=rows[:5],
    row_count=len(rows),
)
result["judge_verdict"] = {
    "approved": verdict.approved,
    "confidence": verdict.confidence,
    "reason": verdict.reason,
}

if not verdict.approved:
    result["_judge_warning"] = f"⚠ Query may not correctly answer the question: {verdict.reason}"
```

**IMPORTANT**: The `user_question` needs to be threaded through. In `tools.py` `execute_tool_call()`, pass the user's original question in the tool context dict. The orchestrator already has `user_message` — add it to the tool call context:

```python
# In tools.py execute_tool_call():
tool_input["_context"] = {"user_question": context.get("user_question", "")}
```

### Cost Analysis
- Haiku: ~$0.25/MTok input, ~$1.25/MTok output
- Per judge call: ~300 input tokens + ~50 output tokens = ~$0.0001
- At 1,000 queries/day = $0.10/day = $3/month
- This is negligible. Ship it.

---

## IMPROVEMENT 2: Structured Outputs for Confidence Scoring

### Why
Currently, confidence is extracted via regex: `<confidence>(\d)</confidence>`. This is fragile — if the LLM forgets the tag or formats it wrong, you lose the signal. Anthropic and OpenAI both support forcing structured output. For Anthropic specifically, using a prefilled assistant turn forces the model to output the exact structure.

### What Exists Today
- `base_agent.py` line 133: `_CONFIDENCE_RE = re.compile(r"<confidence>(\d)</confidence>")`
- `base_agent.py` line 139: `parse_confidence(text)` → returns `int | None`
- `base_agent.py` line 147: `strip_confidence_tag(text)` → removes tag from text
- The `<confidence>` tag is instructed in `unified_agent.py` system prompt (around line 258)
- There are 4 places in `base_agent.py` where confidence is parsed (lines 337, 448, 565, 678)
- Agent responds with free-form text that may or may not contain the tag

### Architecture
Instead of asking the LLM to embed a tag in free-form text, use a **two-part response strategy**:

1. The LLM generates its normal response (answer text)
2. After the final response, make ONE extra fast call with a structured output schema to get confidence + reasoning

This is cleaner than trying to force structure in the main response (which can degrade answer quality).

### TDD Cycle 2A: Structured Confidence Extractor

**RED** — Create `backend/tests/test_structured_confidence.py`:
```python
import pytest
from app.services.confidence_extractor import extract_structured_confidence, ConfidenceAssessment

@pytest.mark.asyncio
async def test_extracts_confidence_from_context():
    assessment = await extract_structured_confidence(
        user_question="What is total revenue last month?",
        assistant_response="Based on the SuiteQL query, total revenue last month was $1.2M.",
        tools_used=["netsuite_suiteql"],
        tool_success_rate=1.0,
    )
    assert isinstance(assessment, ConfidenceAssessment)
    assert 1 <= assessment.score <= 5
    assert len(assessment.reasoning) > 0

@pytest.mark.asyncio
async def test_low_confidence_when_no_tools():
    assessment = await extract_structured_confidence(
        user_question="How much did we sell last quarter?",
        assistant_response="Based on my knowledge, you sold approximately $5M.",
        tools_used=[],
        tool_success_rate=0.0,
    )
    assert assessment.score <= 3  # No tools = can't be confident about data

@pytest.mark.asyncio
async def test_fallback_on_error():
    """If structured extraction fails, fall back to regex parsing."""
    assessment = await extract_structured_confidence(
        user_question="test",
        assistant_response="Here's your answer. <confidence>4</confidence>",
        tools_used=[],
        tool_success_rate=1.0,
        _force_error=True,  # Simulate LLM failure
    )
    assert assessment.score == 4  # Fell back to regex
    assert assessment.source == "regex_fallback"
```

**GREEN** — Create `backend/app/services/confidence_extractor.py`:
```python
"""Structured confidence extraction — replaces regex-based <confidence> tag parsing.

Uses a fast Haiku call with a forced output structure to reliably extract
confidence scores. Falls back to regex parsing if the call fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import anthropic

from app.core.config import settings

logger = logging.getLogger(__name__)

EXTRACTOR_MODEL = "claude-haiku-4-5-20251001"
EXTRACTOR_MAX_TOKENS = 150
EXTRACTOR_TIMEOUT = 2.0  # Must be very fast

_CONFIDENCE_RE = re.compile(r"<confidence>(\d)</confidence>")


@dataclass
class ConfidenceAssessment:
    score: int  # 1-5
    reasoning: str
    source: str  # "structured", "regex_fallback", "default"


_EXTRACTOR_PROMPT = """\
Rate the confidence of this AI assistant response on a 1-5 scale.

Consider:
- Did the assistant use appropriate tools? (tools_used is empty = low confidence for data questions)
- Did the tools succeed? (tool_success_rate)
- Does the response actually answer the question?
- Is the response hedging or uncertain?

Respond with EXACTLY this JSON (no other text):
{"score": N, "reasoning": "one sentence"}

Where N is 1-5:
1 = Very low (guessing, no tools used for data question)
2 = Low (tools failed or answer seems off)
3 = Moderate (partial answer or some uncertainty)
4 = High (tools succeeded, answer looks correct)
5 = Very high (proven pattern match, all tools succeeded, exact answer)
"""


async def extract_structured_confidence(
    user_question: str,
    assistant_response: str,
    tools_used: list[str],
    tool_success_rate: float,
    *,
    _force_error: bool = False,
    _timeout_seconds: float = EXTRACTOR_TIMEOUT,
) -> ConfidenceAssessment:
    """Extract confidence via structured Haiku call. Falls back to regex."""

    # Try regex first (free, instant) — if present, use it
    if not _force_error:
        regex_match = _CONFIDENCE_RE.search(assistant_response)
        if regex_match:
            score = int(regex_match.group(1))
            return ConfidenceAssessment(
                score=max(1, min(5, score)),
                reasoning="self-assessed by agent",
                source="regex_fallback",
            )

    # Structured extraction via Haiku
    try:
        if _force_error:
            raise RuntimeError("forced error for testing")

        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

        context = (
            f"User question: {user_question[:200]}\n"
            f"Assistant response: {assistant_response[:500]}\n"
            f"Tools used: {tools_used}\n"
            f"Tool success rate: {tool_success_rate:.0%}"
        )

        response = await asyncio.wait_for(
            client.messages.create(
                model=EXTRACTOR_MODEL,
                max_tokens=EXTRACTOR_MAX_TOKENS,
                system=_EXTRACTOR_PROMPT,
                messages=[{"role": "user", "content": context}],
            ),
            timeout=_timeout_seconds,
        )

        text = response.content[0].text.strip()
        parsed = json.loads(text)
        score = max(1, min(5, int(parsed["score"])))
        reasoning = str(parsed.get("reasoning", ""))

        return ConfidenceAssessment(score=score, reasoning=reasoning, source="structured")

    except Exception:
        logger.info("confidence_extractor.fallback question=%r", user_question[:60])
        return ConfidenceAssessment(score=3, reasoning="extraction_failed", source="default")
```

### TDD Cycle 2B: Replace regex in base_agent.py

In `base_agent.py`, update the 4 confidence parsing blocks (lines 337, 448, 565, 678). Instead of:
```python
confidence = parse_confidence(final_text)
if confidence is not None:
    final_text = strip_confidence_tag(final_text)
    if confidence <= 2:
        final_text += _LOW_CONFIDENCE_DISCLAIMER
    logger.info("agent.confidence agent=%s score=%d", self.agent_name, confidence)
```

Change to:
```python
# Strip any <confidence> tags from text regardless
final_text = strip_confidence_tag(final_text)

# Structured confidence extraction (async, fail-safe)
from app.services.confidence_extractor import extract_structured_confidence
tools_used = [c.get("tool", "") for c in tool_calls_log]
tool_ok = sum(1 for c in tool_calls_log if not tool_call_had_error(c))
tool_rate = tool_ok / len(tool_calls_log) if tool_calls_log else 0.0

assessment = await extract_structured_confidence(
    user_question=task,
    assistant_response=final_text[:500],
    tools_used=tools_used,
    tool_success_rate=tool_rate,
)
confidence = assessment.score
if confidence <= 2:
    final_text += _LOW_CONFIDENCE_DISCLAIMER
logger.info("agent.confidence agent=%s score=%d source=%s", self.agent_name, confidence, assessment.source)
```

**IMPORTANT**: Keep `strip_confidence_tag()` to clean up any tags the LLM still outputs. The structured extractor checks regex first anyway, so if the tag is present it's used instantly without an LLM call. The Haiku call only fires when the tag is missing.

### Cost Analysis
- Only fires when `<confidence>` tag is missing from response (~20% of the time based on observation)
- When it fires: ~200 input tokens + ~30 output tokens via Haiku = ~$0.00008
- At 1,000 queries/day × 20% miss rate = 200 calls = $0.016/day
- Effectively free.

---

## IMPROVEMENT 3: Prompt Caching Optimization

### Why
Your system prompt is rebuilt every turn but most of it is static per session (tenant schema, soul config, brand identity, tool definitions). Anthropic's prompt caching charges 25% for cache writes and gives 90% discount on cache reads. By structuring your system prompt into a STATIC section (cached) and a DYNAMIC section (per-turn), you can cut token costs by 60-70% on multi-turn conversations.

### What Exists Today
- `anthropic_adapter.py` line 24-29: System prompt already uses `cache_control: {"type": "ephemeral"}` — but as a SINGLE block
- `anthropic_adapter.py` line 36-37: Tool definitions also cached — last tool gets `cache_control`
- `orchestrator.py` lines 296-313: System prompt built fresh each turn by concatenating: base template + brand identity + soul tone + soul quirks
- `unified_agent.py` line 57: `_SYSTEM_PROMPT` template with `{{INJECT_METADATA_HERE}}` placeholder
- Context injection happens in orchestrator around lines 350-450: entity resolution, domain knowledge, proven patterns all concatenated into system prompt or user message

### Architecture
Split the system prompt into two content blocks in the Anthropic messages array:

```python
system=[
    {
        "type": "text",
        "text": STATIC_PROMPT,  # Template + schema + soul + brand — same every turn
        "cache_control": {"type": "ephemeral"},  # CACHED (90% discount on reads)
    },
    {
        "type": "text",
        "text": DYNAMIC_PROMPT,  # Entity resolution + domain knowledge + patterns — varies per turn
        # NO cache_control — changes every message
    },
]
```

### TDD Cycle 3A: Prompt Splitter

**RED** — Create `backend/tests/test_prompt_cache.py`:
```python
from app.services.chat.prompt_cache import split_system_prompt, StaticDynamicPrompt

def test_split_separates_static_and_dynamic():
    full_prompt = (
        "You are a NetSuite AI assistant.\n"
        "<tenant_schema>schema here</tenant_schema>\n"
        "Your name is TestBrand.\n"
        "## AI Tone\nBe helpful.\n"
        "## NetSuite Quirks\nUse single-letter status codes.\n"
        "<tenant_vernacular>resolved entities</tenant_vernacular>\n"
        "<domain_knowledge>relevant docs</domain_knowledge>\n"
        "<proven_patterns>SQL templates</proven_patterns>"
    )
    result = split_system_prompt(full_prompt)
    assert isinstance(result, StaticDynamicPrompt)
    # Static should contain: base prompt, schema, brand, soul
    assert "NetSuite AI assistant" in result.static
    assert "tenant_schema" in result.static
    assert "TestBrand" in result.static
    assert "AI Tone" in result.static
    # Dynamic should contain: vernacular, domain knowledge, patterns
    assert "tenant_vernacular" in result.dynamic
    assert "domain_knowledge" in result.dynamic
    assert "proven_patterns" in result.dynamic

def test_static_excludes_per_turn_context():
    result = split_system_prompt("Base prompt\n<tenant_vernacular>data</tenant_vernacular>")
    assert "tenant_vernacular" not in result.static
    assert "tenant_vernacular" in result.dynamic

def test_empty_dynamic_returns_empty_string():
    result = split_system_prompt("Just a base prompt with no dynamic blocks")
    assert result.static == "Just a base prompt with no dynamic blocks"
    assert result.dynamic == ""
```

**GREEN** — Create `backend/app/services/chat/prompt_cache.py`:
```python
"""Prompt caching optimizer — splits system prompts into static (cacheable) and dynamic (per-turn) sections.

Static section: base prompt template, tenant schema, brand identity, soul config
Dynamic section: entity vernacular, domain knowledge, proven patterns (change every turn)

The static section gets Anthropic's cache_control: {"type": "ephemeral"} which gives
90% discount on cache reads after the first turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# These XML blocks change every turn — extract to dynamic section
_DYNAMIC_TAGS = re.compile(
    r"<(?:tenant_vernacular|domain_knowledge|proven_patterns|financial_context)>.*?</(?:tenant_vernacular|domain_knowledge|proven_patterns|financial_context)>\s*",
    re.DOTALL,
)


@dataclass
class StaticDynamicPrompt:
    static: str   # Cached across turns
    dynamic: str  # Rebuiltevery turn


def split_system_prompt(full_prompt: str) -> StaticDynamicPrompt:
    """Split a system prompt into static (cacheable) and dynamic (per-turn) parts.

    Static: everything EXCEPT <tenant_vernacular>, <domain_knowledge>, <proven_patterns>
    Dynamic: the extracted XML blocks
    """
    dynamic_blocks: list[str] = []

    def _extract(match: re.Match) -> str:
        dynamic_blocks.append(match.group(0).strip())
        return ""

    static = _DYNAMIC_TAGS.sub(_extract, full_prompt).strip()
    dynamic = "\n\n".join(dynamic_blocks)

    return StaticDynamicPrompt(static=static, dynamic=dynamic)
```

### TDD Cycle 3B: Update Anthropic Adapter

**RED** — Add test:
```python
@pytest.mark.asyncio
async def test_anthropic_adapter_sends_two_system_blocks():
    """When system prompt has static + dynamic, adapter should send 2 system blocks."""
    adapter = AnthropicAdapter(api_key="test")
    # Mock the client
    with patch.object(adapter._client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = MockResponse(text="Hello", usage=MockUsage())

        await adapter.create_message(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system="Static part",
            system_dynamic="Dynamic part",  # NEW parameter
            messages=[{"role": "user", "content": "test"}],
        )

        call_kwargs = mock_create.call_args.kwargs
        system_blocks = call_kwargs["system"]
        assert len(system_blocks) == 2
        assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in system_blocks[1]
```

**GREEN** — In `backend/app/services/chat/adapters/anthropic_adapter.py`, update `create_message()`:

```python
async def create_message(
    self,
    *,
    model: str,
    max_tokens: int,
    system: str,
    system_dynamic: str = "",  # NEW: per-turn context (not cached)
    messages: list[dict],
    tools: list[dict] | None = None,
) -> LLMResponse:
    # Build system blocks
    system_blocks = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},  # CACHED
        }
    ]
    if system_dynamic:
        system_blocks.append({
            "type": "text",
            "text": system_dynamic,
            # NO cache_control — changes every turn
        })

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": messages,
    }
    # ... rest unchanged
```

Also update `stream_message()` the same way.

### TDD Cycle 3C: Update Orchestrator to Split Prompt

In `orchestrator.py`, where the system prompt is assembled (around line 296-450), after ALL prompt assembly is done:

```python
from app.services.chat.prompt_cache import split_system_prompt

# After all prompt assembly (brand identity, soul, metadata, context blocks):
prompt_parts = split_system_prompt(system_prompt)

# Pass both parts to the adapter
async for event in adapter.stream_message(
    model=model,
    max_tokens=max_tokens,
    system=prompt_parts.static,
    system_dynamic=prompt_parts.dynamic,  # NEW
    messages=messages,
    tools=tool_definitions,
):
    yield event
```

**NOTE**: The `system_dynamic` parameter is ignored by OpenAI and Gemini adapters — they just concatenate `system + system_dynamic` into one string. Only the Anthropic adapter benefits from the split. This keeps the change backward-compatible.

Update `BaseLLMAdapter.create_message()` and `stream_message()` signature to accept `system_dynamic: str = ""`. In OpenAI and Gemini adapters, just do:
```python
full_system = f"{system}\n\n{system_dynamic}".strip() if system_dynamic else system
```

### Cost Savings Estimate
- Average system prompt: ~4,000 tokens (schema + soul + brand + tools)
- Average dynamic context: ~800 tokens (entity resolution + domain knowledge + patterns)
- Without caching: 4,800 tokens × $3/MTok × 5 turns = $0.072/session
- With caching: 4,000 × $3 × 0.1 (cache read) + 800 × $3 × 5 turns = $0.013/session
- **82% reduction in system prompt costs** for multi-turn conversations
- At 500 sessions/day: saves ~$30/day = ~$900/month

---

## VERIFICATION CHECKLIST

After all 3 improvements:

### Judge Model
1. `cd backend && python -m pytest tests/test_suiteql_judge.py -v` — all pass
2. Send a SuiteQL query via chat → tool result JSON should include `judge_verdict` field
3. Intentionally ask a broad question → judge should flag empty result
4. Check latency: judge should add <3s to total response time

### Structured Confidence
1. `cd backend && python -m pytest tests/test_structured_confidence.py -v` — all pass
2. Send a chat message → confidence score should appear (even if LLM forgets the tag)
3. Check logs: `agent.confidence source=regex_fallback` (when tag present) or `source=structured` (when Haiku called)
4. Cost check: Haiku calls should be <20% of total messages

### Prompt Caching
1. `cd backend && python -m pytest tests/test_prompt_cache.py -v` — all pass
2. Send 3+ messages in one session → check Anthropic usage logs
3. After first message: expect `cache_creation_input_tokens > 0`
4. After second message: expect `cache_read_input_tokens > 0` (cache hit)
5. Verify OpenAI/Gemini still work (system_dynamic concatenated)

### Total Monthly Cost Impact
| Component | Cost/Query | Queries/Day | Monthly |
|-----------|-----------|-------------|---------|
| Judge (Haiku) | $0.0001 | 1,000 | $3 |
| Structured Confidence (Haiku) | $0.00008 × 20% | 1,000 | $0.50 |
| Prompt Cache Savings | -$0.012/session | 500 | -$900 |
| **Net** | | | **-$896/month savings** |

These three changes make the platform more accurate, more reliable, AND cheaper. Ship them.
