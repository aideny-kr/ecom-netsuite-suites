# Confidence Score System — TDD Implementation Prompt

> Copy-paste this into Claude Code. It implements a composite confidence scoring system using Red-Green-Refactor TDD across 11 cycles.

---

## TASK

Implement a **composite confidence score** for the AI chat platform using strict **TDD (Red-Green-Refactor)** methodology. For each cycle: write a FAILING test first (RED), write MINIMAL code to make it pass (GREEN), then refactor if needed.

Read `CLAUDE.md` for project patterns before starting. Follow all conventions exactly.

---

## WHAT EXISTS TODAY

- `base_agent.py` line 138: `parse_confidence(text)` extracts `<confidence>N</confidence>` tag (1-5 int)
- `base_agent.py` line 146: `strip_confidence_tag(text)` removes the tag
- `base_agent.py` lines 337-342, 448-453, 565-570, 678-683: Each agent path parses confidence, logs it, appends disclaimer if ≤2
- `AgentResult` dataclass at line 194: has `success`, `data`, `error`, `tool_calls_log`, `tokens_used`, `agent_name` — NO confidence_score field
- `ChatMessage` model in `models/chat.py`: NO confidence_score column
- `_serialize_message()` in `api/v1/chat.py` line 91: does NOT include confidence
- SSE stream supports types: "text", "tool_status", "message", "error" — NO "confidence" type
- Frontend `ChatMessage` interface in `lib/types.ts`: NO confidence_score field
- Latest migration: 038_chat_message_content_summary

---

## COMPOSITE SCORING ALGORITHM

Create `backend/app/services/confidence_service.py` with a `CompositeScorer` dataclass:

```python
@dataclass
class CompositeScorer:
    # Input signals (all 0.0-1.0 except success_count)
    llm_score: float = 0.0           # LLM self-assessment, already normalized to 0-1
    query_pattern_similarity: float = 0.0
    query_pattern_success_count: int = 0
    domain_knowledge_similarity: float = 0.0
    entity_resolution_confidence: float = 0.0
    tool_success_rate: float = 0.0    # successful_calls / total_calls
    num_tool_calls: int = 0
    required_tool_calls: bool = False  # True if query needs tools

    def compute(self) -> float:
        """Return composite confidence score 1.0-5.0."""
```

**Weights:**
- LLM self-assessment: 0.40
- Query pattern similarity: 0.15
- Pattern success boost: 0.10 (min(success_count / 50, 0.1))
- Domain knowledge similarity: 0.10
- Entity resolution confidence: 0.15
- Tool success rate: 0.10

**Penalties:**
- If `required_tool_calls=True` and `num_tool_calls=0`: penalty −0.2
- If `num_tool_calls > 0` and `tool_success_rate < 1.0`: penalty −0.3

**Final formula:** `round(clamp(weighted_sum + penalties, 0, 1) * 4 + 1, 1)` → 1.0-5.0

---

## TDD CYCLES — EXECUTE IN ORDER

### CYCLE 1: Scoring Service (RED → GREEN → REFACTOR)

**RED** — Create `backend/tests/test_confidence_service.py`:
```python
import pytest
from app.services.confidence_service import CompositeScorer

def test_all_perfect_signals_returns_5():
    scorer = CompositeScorer(
        llm_score=1.0, query_pattern_similarity=1.0,
        query_pattern_success_count=50, domain_knowledge_similarity=1.0,
        entity_resolution_confidence=1.0, tool_success_rate=1.0,
        num_tool_calls=3,
    )
    assert scorer.compute() == 5.0

def test_all_zero_signals_returns_1():
    scorer = CompositeScorer()
    assert scorer.compute() == 1.0

def test_llm_only_high_returns_moderate():
    scorer = CompositeScorer(llm_score=1.0)
    result = scorer.compute()
    assert 2.5 <= result <= 3.0  # 0.4 weight * 4 + 1 = 2.6

def test_missing_tools_penalty():
    scorer = CompositeScorer(llm_score=0.8, required_tool_calls=True, num_tool_calls=0)
    result = scorer.compute()
    no_penalty = CompositeScorer(llm_score=0.8, required_tool_calls=False, num_tool_calls=0)
    assert result < no_penalty.compute()

def test_tool_failure_penalty():
    scorer = CompositeScorer(
        llm_score=0.8, tool_success_rate=0.33, num_tool_calls=3,
    )
    result = scorer.compute()
    perfect = CompositeScorer(
        llm_score=0.8, tool_success_rate=1.0, num_tool_calls=3,
    )
    assert result < perfect.compute()

def test_score_clamped_to_1_5_range():
    # Even with extreme penalties
    scorer = CompositeScorer(
        llm_score=0.0, required_tool_calls=True, num_tool_calls=0,
        tool_success_rate=0.0,
    )
    result = scorer.compute()
    assert result >= 1.0
    assert result <= 5.0

def test_pattern_success_boost_caps_at_01():
    scorer = CompositeScorer(query_pattern_success_count=1000)
    scorer2 = CompositeScorer(query_pattern_success_count=50)
    # Both should have same boost (capped)
    assert scorer.compute() == scorer2.compute()
```

Run: `cd backend && python -m pytest tests/test_confidence_service.py -v` — should FAIL (import error).

**GREEN** — Create `backend/app/services/confidence_service.py` implementing `CompositeScorer` with the algorithm above. Run tests — all should PASS.

**REFACTOR** — Add `logger.info()` with all signal values and final score. Add docstrings.

---

### CYCLE 2: AgentResult Enhancement (RED → GREEN)

**RED** — Create `backend/tests/test_agent_result.py`:
```python
from app.services.chat.agents.base_agent import AgentResult

def test_agent_result_accepts_confidence_score():
    result = AgentResult(success=True, data="text", confidence_score=4.2)
    assert result.confidence_score == 4.2

def test_agent_result_confidence_defaults_none():
    result = AgentResult(success=True, data="text")
    assert result.confidence_score is None
```

Run: `python -m pytest tests/test_agent_result.py -v` — should FAIL.

**GREEN** — In `backend/app/services/chat/agents/base_agent.py`, add to `AgentResult` dataclass (after line 202):
```python
confidence_score: float | None = None
```

Run tests — PASS.

---

### CYCLE 3: Unified Agent Integration (RED → GREEN → REFACTOR)

**RED** — Create `backend/tests/test_unified_agent_confidence.py`:
```python
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.chat.agents.base_agent import AgentResult

@pytest.mark.asyncio
async def test_unified_agent_sets_confidence_on_result():
    """After completing, unified agent should have a confidence_score."""
    # This test verifies the integration point — mock the LLM to return
    # a response with a <confidence> tag, and verify AgentResult has score
    from app.services.chat.agents.unified_agent import UnifiedAgent
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage

    mock_adapter = MagicMock()
    mock_adapter.create_message = AsyncMock(return_value=LLMResponse(
        text_blocks=["Revenue last month was $1.2M. <confidence>4</confidence>"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    ))

    # Patch DB operations and tool execution
    with patch("app.services.chat.agents.unified_agent.execute_tool_call", new_callable=AsyncMock), \
         patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock):
        agent = UnifiedAgent.__new__(UnifiedAgent)
        agent.tenant_id = uuid.uuid4()
        agent.user_id = uuid.uuid4()
        agent.agent_name = "unified"
        agent.max_steps = 10
        agent.tool_names = frozenset()
        agent._correlation_id = "test"

        result = await agent.run(
            task="What is revenue last month?",
            context={"entity_resolution_confidence": 0.95, "domain_knowledge_similarity": 0.8},
            db=AsyncMock(),
            adapter=mock_adapter,
        )

        assert result.confidence_score is not None
        assert 1.0 <= result.confidence_score <= 5.0
```

Run — FAIL.

**GREEN** — In `unified_agent.py`, after the agentic loop completes and before returning `AgentResult`:
1. Parse `<confidence>` tag from final text
2. Collect signals from `context` dict (entity_resolution_confidence, domain_knowledge_similarity, matched_pattern_similarity, matched_pattern_success_count)
3. Compute tool_success_rate from tool_calls_log
4. Call `CompositeScorer(...).compute()`
5. Set on `AgentResult(confidence_score=score)`

Also do the same for `run_streaming()`.

**REFACTOR** — Extract `_collect_confidence_signals(context, tool_calls_log, llm_confidence)` helper.

---

### CYCLE 4: Database Migration (RED → GREEN)

**RED** — Verify migration file doesn't exist yet.

**GREEN** — Create `backend/alembic/versions/039_chat_message_confidence_score.py`:
```python
"""Add confidence_score to chat_messages."""
from alembic import op
import sqlalchemy as sa

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("confidence_score", sa.Numeric(precision=3, scale=1), nullable=True))

def downgrade() -> None:
    op.drop_column("chat_messages", "confidence_score")
```

Run: `cd backend && alembic upgrade head` — verify column exists.

---

### CYCLE 5: ChatMessage Model (RED → GREEN)

**GREEN** — In `backend/app/models/chat.py`, add after `content_summary` (line 47):
```python
from sqlalchemy import Numeric
# ...
confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(precision=3, scale=1), nullable=True)
```

Add `from decimal import Decimal` at top of file.

---

### CYCLE 6: Orchestrator SSE + Persistence (RED → GREEN → REFACTOR)

**RED** — Create `backend/tests/test_orchestrator_confidence.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_sse_stream_includes_confidence_event():
    """run_chat_turn should yield a confidence event."""
    # Collect all SSE events from run_chat_turn
    # Assert one has type="confidence" with score in 1-5 range
    # Assert final message event has confidence_score field
    pass  # Implement with your standard mock pattern from test_chat_orchestrator.py
```

**GREEN** — In `orchestrator.py`, in both the unified agent path and coordinator path:
1. After agent completes but before yielding the final "message" event:
```python
if hasattr(agent_result, 'confidence_score') and agent_result.confidence_score is not None:
    yield {"type": "confidence", "score": float(agent_result.confidence_score), "explanation": _get_confidence_explanation(agent_result.confidence_score)}
```
2. When creating `ChatMessage`, add: `confidence_score=agent_result.confidence_score if hasattr(agent_result, 'confidence_score') else None`

**REFACTOR** — Add helper:
```python
def _get_confidence_explanation(score: float) -> str:
    if score >= 4.5: return "Very high confidence — used proven patterns and all tools succeeded"
    if score >= 3.5: return "High confidence — data looks correct"
    if score >= 2.5: return "Moderate confidence — results may need verification"
    return "Low confidence — please verify this data before acting on it"
```

---

### CYCLE 7: API Serialization (RED → GREEN)

**RED** — Create `backend/tests/test_chat_api_confidence.py`:
```python
from app.api.v1.chat import _serialize_message

def test_serialize_includes_confidence_when_present(mock_message):
    mock_message.confidence_score = 4.2
    result = _serialize_message(mock_message)
    assert result["confidence_score"] == 4.2

def test_serialize_excludes_confidence_when_none(mock_message):
    mock_message.confidence_score = None
    result = _serialize_message(mock_message)
    assert "confidence_score" not in result
```

**GREEN** — In `backend/app/api/v1/chat.py`, add to `_serialize_message()` before `return result`:
```python
if msg.confidence_score is not None:
    result["confidence_score"] = float(msg.confidence_score)
```

---

### CYCLE 8: Frontend TypeScript Types

In `frontend/src/lib/types.ts`, add to `ChatMessage` interface:
```typescript
confidence_score?: number;
```

---

### CYCLE 9: Frontend SSE Parser

In `frontend/src/lib/chat-stream.ts`:

1. Add to `ChatStreamEvent` union type:
```typescript
| { type: "confidence"; score: number; explanation: string }
```

2. Add to `normalizeStreamEvent()`:
```typescript
if (type === "confidence" && typeof data.score === "number") {
    return { type: "confidence", score: data.score, explanation: String(data.explanation || "") };
}
```

3. Add `onConfidence` handler to `StreamHandlers` type and `consumeChatStream()`.

---

### CYCLE 10: ConfidenceBadge Component

Create `frontend/src/components/chat/confidence-badge.tsx`:

```tsx
"use client";

import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";

interface ConfidenceBadgeProps {
  score: number;
  explanation?: string;
}

function getScoreStyle(score: number) {
  if (score >= 4.5) return { bg: "bg-emerald-100", text: "text-emerald-700", label: "Very High" };
  if (score >= 3.5) return { bg: "bg-sky-100", text: "text-sky-700", label: "High" };
  if (score >= 2.5) return { bg: "bg-yellow-100", text: "text-yellow-700", label: "Medium" };
  return { bg: "bg-orange-100", text: "text-orange-700", label: "Low" };
}

export function ConfidenceBadge({ score, explanation }: ConfidenceBadgeProps) {
  const style = getScoreStyle(score);
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${style.bg} ${style.text}`}>
            <span className="opacity-60">Confidence</span>
            {score.toFixed(1)}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs">
          <p className="text-[13px] font-medium">{style.label} Confidence ({score.toFixed(1)}/5.0)</p>
          {explanation && <p className="text-[12px] text-muted-foreground mt-1">{explanation}</p>}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
```

---

### CYCLE 11: Message List Integration

In `frontend/src/components/chat/message-list.tsx`:

1. Import: `import { ConfidenceBadge } from "./confidence-badge";`
2. In the assistant message render block, after the message content and after tool call cards, add:
```tsx
{message.role === "assistant" && message.confidence_score != null && (
  <div className="mt-2">
    <ConfidenceBadge score={message.confidence_score} />
  </div>
)}
```

---

## SIGNAL THREADING

The orchestrator already runs concurrent context assembly. Thread these signals through the `context` dict:

1. **In `tenant_resolver.py`** — after `func.similarity()` query, add to context:
   ```python
   context["entity_resolution_confidence"] = max(score for _, score in resolved) if resolved else 0.0
   ```

2. **In orchestrator.py concurrent assembly** — after `retrieve_similar_patterns()`, add to context:
   ```python
   context["matched_pattern_similarity"] = patterns[0]["similarity"] if patterns else 0.0
   context["matched_pattern_success_count"] = patterns[0]["success_count"] if patterns else 0
   ```

3. **In orchestrator.py concurrent assembly** — after `retrieve_domain_knowledge()`, add to context:
   ```python
   sims = [c["similarity"] for c in domain_chunks if c.get("similarity")]
   context["domain_knowledge_similarity"] = sum(sims) / len(sims) if sims else 0.0
   ```

---

## VERIFICATION CHECKLIST

After all cycles:
1. `cd backend && python -m pytest tests/test_confidence_service.py tests/test_agent_result.py tests/test_chat_api_confidence.py -v` — all pass
2. `cd backend && alembic upgrade head` — 039 applied
3. Send a chat message via the UI → check SSE stream in browser DevTools Network tab for `{"type":"confidence",...}`
4. Query DB: `SELECT id, confidence_score FROM chat_messages WHERE role='assistant' ORDER BY created_at DESC LIMIT 5` — non-null scores
5. UI shows colored confidence badge on assistant messages
6. Ask a vague question with no matching patterns → badge should be yellow/orange (< 3.0)
7. Ask a question matching a proven pattern → badge should be green (≥ 4.0)
