# User Feedback Loop — Anti-Hallucination Sprint 3 (TDD)

> Adds thumbs up/down on assistant messages so the proven pattern system gets
> real signal from users instead of treating "query returned rows" as success.
> Patterns marked "not helpful" are demoted. Patterns marked "helpful" are boosted.
>
> Use Red-Green-Refactor TDD for each cycle.

Read `CLAUDE.md` before starting. Follow all conventions exactly.

---

## Why This Matters

Today, `extract_and_store_pattern()` stores ANY query that returns rows as a
"proven pattern" with auto-incrementing `success_count`. A query that returns
wrong data still looks successful. Users have no way to say "this answer was wrong."
This means the system can learn and reinforce bad patterns.

## What Exists Today

- `tenant_query_patterns` table: `user_question`, `working_sql`, `intent_embedding`, `success_count`, `last_used_at`
- `query_pattern_service.py`: `extract_and_store_pattern()` auto-stores on any successful query (rows > 0)
- `query_pattern_service.py`: `retrieve_similar_patterns()` returns top-K by pgvector cosine similarity
- `base_agent.py` line 154: `_maybe_store_query_pattern()` fires after every successful agent response
- `chat.py` model: `ChatMessage` with `confidence_score`, `query_importance`, `tool_calls` (JSON)
- `chat.py` API: `PATCH /messages/{id}/importance` (admin-only importance override)
- No thumbs up/down, no `user_feedback` column, no feedback API endpoint
- Frontend: `confidence-badge.tsx` and `importance-badge.tsx` are read-only

---

## TDD Cycles (7 cycles, 3 phases)

### Phase 1: Backend Schema + API

**Cycle 1 — Database Migration**

RED — Test column exists:
```python
def test_chat_message_has_user_feedback():
    from app.models.chat import ChatMessage
    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="test",
        user_feedback="helpful",
    )
    assert msg.user_feedback == "helpful"

def test_chat_message_feedback_defaults_none():
    from app.models.chat import ChatMessage
    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="test",
    )
    assert msg.user_feedback is None
```

GREEN:

1. Add to `backend/app/models/chat.py` after the `query_importance` line:
```python
    # User feedback: "helpful", "not_helpful", or None
    user_feedback: Mapped[str | None] = mapped_column(String(20), nullable=True)
```

2. Create migration `backend/alembic/versions/041_user_feedback.py`:
```python
"""041_user_feedback"""
from alembic import op
import sqlalchemy as sa

revision = "041_user_feedback"
down_revision = "040_query_importance"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("user_feedback", sa.String(20), nullable=True))

def downgrade() -> None:
    op.drop_column("chat_messages", "user_feedback")
```

REFACTOR: None needed.

---

**Cycle 2 — Feedback API Endpoint**

RED — Create `backend/tests/test_chat_feedback.py`:
```python
import pytest
import uuid
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_set_feedback_helpful(client: AsyncClient, admin_user, db):
    """PATCH feedback with 'helpful' should update message."""
    # Create a session and message first
    session_resp = await client.post(
        "/api/v1/chat/sessions",
        json={"title": "Test"},
        headers=admin_user["headers"],
    )
    session_id = session_resp.json()["id"]

    # Insert an assistant message directly
    from app.models.chat import ChatMessage
    msg = ChatMessage(
        tenant_id=admin_user["tenant_id"],
        session_id=uuid.UUID(session_id),
        role="assistant",
        content="Total orders: 42",
        tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": "SELECT COUNT(*) FROM transaction"}}],
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    # Set feedback
    resp = await client.patch(
        f"/api/v1/chat/messages/{msg.id}/feedback",
        params={"feedback": "helpful"},
        headers=admin_user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["feedback"] == "helpful"

@pytest.mark.asyncio
async def test_set_feedback_not_helpful(client: AsyncClient, admin_user, db):
    # Similar setup...
    # Set feedback to not_helpful
    resp = await client.patch(
        f"/api/v1/chat/messages/{msg.id}/feedback",
        params={"feedback": "not_helpful"},
        headers=admin_user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["feedback"] == "not_helpful"

@pytest.mark.asyncio
async def test_set_feedback_invalid_value(client: AsyncClient, admin_user, db):
    # Try invalid feedback value
    resp = await client.patch(
        f"/api/v1/chat/messages/{msg.id}/feedback",
        params={"feedback": "maybe"},
        headers=admin_user["headers"],
    )
    assert resp.status_code == 422  # Validation error

@pytest.mark.asyncio
async def test_set_feedback_wrong_tenant(client: AsyncClient, admin_user, admin_user_b, db):
    # User B should not be able to set feedback on User A's message
    # ... create message for tenant A ...
    resp = await client.patch(
        f"/api/v1/chat/messages/{msg.id}/feedback",
        params={"feedback": "helpful"},
        headers=admin_user_b["headers"],
    )
    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_set_feedback_nonexistent_message(client: AsyncClient, admin_user):
    fake_id = str(uuid.uuid4())
    resp = await client.patch(
        f"/api/v1/chat/messages/{fake_id}/feedback",
        params={"feedback": "helpful"},
        headers=admin_user["headers"],
    )
    assert resp.status_code == 404
```

GREEN — Add to `backend/app/api/v1/chat.py`:

```python
from fastapi import Query as FastAPIQuery

@router.patch("/messages/{message_id}/feedback", status_code=status.HTTP_200_OK)
async def set_message_feedback(
    message_id: str,
    feedback: str = FastAPIQuery(..., pattern="^(helpful|not_helpful)$"),
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Set user feedback (helpful/not_helpful) on an assistant message.

    Any authenticated user can set feedback on messages in their tenant.
    This updates the proven pattern system — helpful boosts patterns,
    not_helpful demotes them.
    """
    try:
        message_uuid = uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid message ID")

    msg = await db.get(ChatMessage, message_uuid)
    if not msg or msg.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    old_feedback = msg.user_feedback
    msg.user_feedback = feedback

    # Update proven patterns based on feedback
    from app.services.query_pattern_service import process_feedback
    await process_feedback(
        db=db,
        tenant_id=user.tenant_id,
        message=msg,
        feedback=feedback,
    )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="chat",
        action="chat.feedback",
        actor_id=user.id,
        resource_type="message",
        resource_id=str(message_id),
        metadata={"feedback": feedback, "old_feedback": old_feedback},
    )
    await db.commit()

    return {"id": str(msg.id), "feedback": feedback}
```

**IMPORTANT**: This endpoint uses `get_current_user` (not `require_permission`), so any
authenticated user in the tenant can give feedback. This is intentional — feedback should
be frictionless.

REFACTOR: None needed.

---

**Cycle 3 — API Serialization**

RED — Test message includes feedback in response:
```python
def test_serialize_message_includes_feedback():
    # Verify _serialize_message returns user_feedback when present
    pass
```

GREEN — Add to `_serialize_message()` in `backend/app/api/v1/chat.py`:
```python
    if msg.user_feedback is not None:
        result["user_feedback"] = msg.user_feedback
```

REFACTOR: None needed.

---

### Phase 2: Pattern Service Integration

**Cycle 4 — Feedback Processing**

RED — Add to `backend/tests/test_chat_feedback.py`:
```python
import pytest
from app.services.query_pattern_service import process_feedback

@pytest.mark.asyncio
async def test_helpful_feedback_increments_pattern(db):
    """Helpful feedback should increment success_count on matching pattern."""
    from app.models.tenant_query_pattern import TenantQueryPattern
    from app.models.chat import ChatMessage

    tenant_id = uuid.UUID("bf92d059-0000-0000-0000-000000000000")
    sql = "SELECT COUNT(*) as cnt FROM transaction WHERE type = 'SalesOrd'"

    # Create a pattern
    pattern = TenantQueryPattern(
        tenant_id=tenant_id,
        user_question="how many sales orders",
        working_sql=sql,
        success_count=3,
    )
    db.add(pattern)
    await db.commit()

    # Create a message with matching tool call
    msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        role="assistant",
        content="There are 42 sales orders.",
        tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": sql}}],
    )
    db.add(msg)
    await db.commit()

    # Process helpful feedback
    await process_feedback(db, tenant_id, msg, "helpful")
    await db.commit()

    await db.refresh(pattern)
    assert pattern.success_count == 4  # Was 3, now 4

@pytest.mark.asyncio
async def test_not_helpful_decrements_pattern(db):
    """Not helpful feedback should decrement success_count."""
    # Similar setup with success_count=3
    # ... process not_helpful ...
    await db.refresh(pattern)
    assert pattern.success_count == 2  # Was 3, now 2

@pytest.mark.asyncio
async def test_not_helpful_floors_at_zero(db):
    """success_count should never go below 0."""
    # Pattern with success_count=0
    # ... process not_helpful ...
    await db.refresh(pattern)
    assert pattern.success_count == 0  # Stays at 0

@pytest.mark.asyncio
async def test_no_matching_pattern_is_noop(db):
    """If no pattern matches the message's SQL, feedback is a no-op."""
    # Message with SQL that has no stored pattern
    # ... process helpful ...
    # Should not raise, just do nothing

@pytest.mark.asyncio
async def test_message_without_tool_calls_is_noop(db):
    """Messages without tool_calls have no patterns to update."""
    msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=...,
        role="assistant",
        content="Here is some documentation...",
        tool_calls=None,
    )
    # Should not raise
    await process_feedback(db, tenant_id, msg, "helpful")
```

GREEN — Add to `backend/app/services/query_pattern_service.py`:
```python
async def process_feedback(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    message: "ChatMessage",
    feedback: str,
) -> None:
    """Update pattern success_count based on user feedback.

    - "helpful" → increment success_count
    - "not_helpful" → decrement success_count (floor at 0)
    """
    if not message.tool_calls:
        return

    from app.models.tenant_query_pattern import TenantQueryPattern

    for call in message.tool_calls:
        if call.get("tool") != "netsuite_suiteql":
            continue

        sql = call.get("params", {}).get("query", "")
        if not sql:
            continue

        # Find matching pattern by SQL
        result = await db.execute(
            select(TenantQueryPattern).where(
                TenantQueryPattern.tenant_id == tenant_id,
                TenantQueryPattern.working_sql == sql,
            )
        )
        pattern = result.scalar_one_or_none()
        if not pattern:
            continue

        if feedback == "helpful":
            pattern.success_count += 1
            pattern.last_used_at = func.now()
        elif feedback == "not_helpful":
            pattern.success_count = max(0, pattern.success_count - 1)
```

REFACTOR: None needed.

---

**Cycle 5 — Pattern Retrieval Filter**

RED — Add to test file:
```python
@pytest.mark.asyncio
async def test_zero_success_patterns_excluded(db):
    """Patterns with success_count=0 should not appear in retrieval."""
    from app.services.query_pattern_service import retrieve_similar_patterns

    tenant_id = uuid.UUID("bf92d059-0000-0000-0000-000000000000")

    # Create pattern with success_count=0
    pattern = TenantQueryPattern(
        tenant_id=tenant_id,
        user_question="bad query example",
        working_sql="SELECT * FROM nonexistent",
        success_count=0,
        intent_embedding=[0.1] * 1536,  # Dummy embedding
    )
    db.add(pattern)
    await db.commit()

    results = await retrieve_similar_patterns(db, tenant_id, "bad query example")
    sql_list = [r["sql"] for r in results]
    assert "SELECT * FROM nonexistent" not in sql_list

@pytest.mark.asyncio
async def test_high_success_patterns_ranked_higher(db):
    """Patterns with higher success_count should appear first."""
    # Create two patterns with different success counts
    # Both should match the query, but higher success should rank higher
    pass
```

GREEN — Modify `retrieve_similar_patterns()` in `backend/app/services/query_pattern_service.py`:

Add a WHERE filter to exclude unreliable patterns:
```python
    # In the SQL query, add:
    # AND success_count > 0
```

Find the existing pgvector query and add the filter. The existing query should look something like:
```sql
    SELECT ... FROM tenant_query_patterns
    WHERE tenant_id = :tenant_id
      AND intent_embedding IS NOT NULL
      AND success_count > 0  -- ADD THIS LINE
    ORDER BY intent_embedding <=> :embedding
    LIMIT :top_k
```

REFACTOR: None needed.

---

### Phase 3: Frontend

**Cycle 6 — TypeScript Types + Feedback Hook**

RED — Type check: `ChatMessage.user_feedback` should be valid.

GREEN:

1. Modify `frontend/src/lib/types.ts` — add to ChatMessage interface:
```typescript
    user_feedback?: "helpful" | "not_helpful" | null;
```

2. Create `frontend/src/hooks/use-chat-feedback.ts`:
```typescript
"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface FeedbackPayload {
    messageId: string;
    feedback: "helpful" | "not_helpful";
}

export function useChatFeedback() {
    const queryClient = useQueryClient();

    return useMutation({
        mutationFn: ({ messageId, feedback }: FeedbackPayload) =>
            apiClient.patch(`/api/v1/chat/messages/${messageId}/feedback?feedback=${feedback}`),
        onSuccess: () => {
            // Invalidate session queries to refresh message data
            queryClient.invalidateQueries({ queryKey: ["chat_sessions"] });
        },
    });
}
```

REFACTOR: None needed.

---

**Cycle 7 — Feedback Buttons in Message List**

RED — Test thumbs up/down render on assistant messages with tool_calls:
```typescript
import { render, screen } from "@testing-library/react";

describe("FeedbackButtons", () => {
    it("renders thumbs on assistant messages with tool_calls", () => {
        render(<MessageItem message={{ role: "assistant", tool_calls: [...], content: "..." }} />);
        expect(screen.getByLabelText("Helpful")).toBeInTheDocument();
        expect(screen.getByLabelText("Not helpful")).toBeInTheDocument();
    });

    it("does not render thumbs on user messages", () => {
        render(<MessageItem message={{ role: "user", content: "..." }} />);
        expect(screen.queryByLabelText("Helpful")).not.toBeInTheDocument();
    });

    it("does not render thumbs on assistant messages without tool_calls", () => {
        render(<MessageItem message={{ role: "assistant", content: "docs...", tool_calls: null }} />);
        expect(screen.queryByLabelText("Helpful")).not.toBeInTheDocument();
    });

    it("disables buttons after feedback is set", () => {
        render(<MessageItem message={{ role: "assistant", tool_calls: [...], user_feedback: "helpful" }} />);
        expect(screen.getByLabelText("Helpful")).toBeDisabled();
    });
});
```

GREEN — Modify `frontend/src/components/chat/message-list.tsx`:

1. Import the hook and icons:
```typescript
import { useChatFeedback } from "@/hooks/use-chat-feedback";
import { ThumbsUp, ThumbsDown } from "lucide-react";
```

2. Add feedback buttons after the message content for assistant messages with tool_calls:
```tsx
{message.role === "assistant" && message.tool_calls && message.tool_calls.length > 0 && (
    <div className="flex items-center gap-1.5 mt-2">
        <button
            onClick={() => feedbackMutation.mutate({ messageId: message.id, feedback: "helpful" })}
            disabled={message.user_feedback !== null && message.user_feedback !== undefined}
            aria-label="Helpful"
            className={cn(
                "p-1 rounded-md text-muted-foreground transition-colors",
                message.user_feedback === "helpful"
                    ? "text-emerald-500 bg-emerald-50 dark:bg-emerald-950/30"
                    : "hover:text-foreground hover:bg-muted",
                message.user_feedback !== null && message.user_feedback !== undefined && message.user_feedback !== "helpful"
                    && "opacity-40 cursor-not-allowed"
            )}
        >
            <ThumbsUp className="h-3.5 w-3.5" />
        </button>
        <button
            onClick={() => feedbackMutation.mutate({ messageId: message.id, feedback: "not_helpful" })}
            disabled={message.user_feedback !== null && message.user_feedback !== undefined}
            aria-label="Not helpful"
            className={cn(
                "p-1 rounded-md text-muted-foreground transition-colors",
                message.user_feedback === "not_helpful"
                    ? "text-rose-500 bg-rose-50 dark:bg-rose-950/30"
                    : "hover:text-foreground hover:bg-muted",
                message.user_feedback !== null && message.user_feedback !== undefined && message.user_feedback !== "not_helpful"
                    && "opacity-40 cursor-not-allowed"
            )}
        >
            <ThumbsDown className="h-3.5 w-3.5" />
        </button>
    </div>
)}
```

REFACTOR: Extract feedback buttons into a `<FeedbackButtons>` component if message-list.tsx gets too large.

---

## Files to Create (3 new)

| File | Purpose |
|------|---------|
| `backend/alembic/versions/041_user_feedback.py` | Add `user_feedback` column to chat_messages |
| `backend/tests/test_chat_feedback.py` | API + pattern integration tests |
| `frontend/src/hooks/use-chat-feedback.ts` | Mutation hook for PATCH endpoint |

## Files to Modify (5 existing)

| File | Change |
|------|--------|
| `backend/app/models/chat.py` | Add `user_feedback: Mapped[str \| None]` column |
| `backend/app/api/v1/chat.py` | Add PATCH feedback endpoint + serialize feedback |
| `backend/app/services/query_pattern_service.py` | Add `process_feedback()` + filter `success_count > 0` |
| `frontend/src/lib/types.ts` | Add `user_feedback` to ChatMessage type |
| `frontend/src/components/chat/message-list.tsx` | Add ThumbsUp/ThumbsDown buttons |

## Dependencies

- Sprint 1 (query importance) should be done first (migration 040 is down_revision for 041)
- Uses existing `query_pattern_service.py` pattern matching
- Uses existing `audit_service.log_event()`

## Verification

1. `alembic upgrade head` — `user_feedback` column added
2. `pytest backend/tests/test_chat_feedback.py -v` — all tests pass
3. Send a data query, get a result with tool_calls. Thumbs up/down buttons appear
4. Click thumbs down → API returns 200, pattern `success_count` decrements
5. Click thumbs up on another message → `success_count` increments
6. Verify: `SELECT success_count FROM tenant_query_patterns ORDER BY last_used_at DESC LIMIT 5`
7. Verify: pattern with `success_count=0` stops appearing in proven patterns context
8. Buttons disabled after clicking (one feedback per message)
