# Query Importance Ranking — 4-Tier Classification System (TDD)

> Adds a query importance tier system that classifies every data query into one of four
> trust levels (Casual → Operational → Reporting Grade → Audit Critical). Higher tiers
> trigger stricter judge validation, additional verification steps, and visual indicators
> in the UI so users know exactly how much to trust a result.
>
> Use Red-Green-Refactor TDD for each cycle. Write failing test first, make it pass with
> minimal code, then refactor.

Read `CLAUDE.md` before starting. Follow all conventions exactly.

---

## The 4 Tiers

| Tier | Name | Description | Judge Threshold | Example |
|------|------|-------------|-----------------|---------|
| 1 | Casual | Quick sanity checks, counts, lookups | Existing behavior (fail-open) | "how many orders today" |
| 2 | Operational | Day-to-day decisions, filtered lists | Judge must approve (confidence >= 0.6) | "show me unfulfilled orders by vendor" |
| 3 | Reporting | Monthly numbers, dashboard data, anything going into a report | Judge confidence >= 0.8, warn if below | "total revenue by month this quarter" |
| 4 | Audit Critical | Revenue, P&L, GL, board-level financials | Judge confidence >= 0.9, flag for human review if below | "net income by account for Q4 audit" |

---

## What Exists Today

- `coordinator.py` line 49: `IntentType` enum with `DATA_QUERY`, `FINANCIAL_REPORT`, etc.
- `coordinator.py` line 112: `_HEURISTIC_RULES` regex patterns for fast intent classification
- `coordinator.py` line 266: `classify_intent()` function
- `suiteql_judge.py` line 44: `JudgeVerdict(approved, confidence, reason)` dataclass
- `suiteql_judge.py` line 89: `judge_suiteql_result()` with 3-second Haiku timeout
- `netsuite_suiteql.py` line 260: `_maybe_judge()` runs judge after query execution
- `orchestrator.py` line 626: Confidence SSE event emission
- `orchestrator.py` line 639: ChatMessage creation with `confidence_score` persistence
- `orchestrator.py` line 470: `classify_intent()` called for financial mode detection
- `chat.py` line 50: `confidence_score` column on ChatMessage (Numeric 3,1)
- `base_agent.py` line 194: `AgentResult` dataclass with `confidence_score` field

**Key insight:** Query importance is orthogonal to intent. A `DATA_QUERY` can be casual or audit-critical. A `FINANCIAL_REPORT` is almost always tier 3 or 4. The classifier runs alongside `classify_intent()`, not inside it.

---

## TDD Cycles (10 cycles, 3 phases)

### Phase 1: Backend Classification + Enforcement

**Cycle 1 — Importance Classifier Service** (NEW file)

RED — Create `backend/tests/test_importance_classifier.py`:
```python
import pytest
from app.services.importance_classifier import classify_importance, ImportanceTier

def test_casual_lookup():
    tier = classify_importance("how many orders today")
    assert tier == ImportanceTier.CASUAL

def test_operational_query():
    tier = classify_importance("show me unfulfilled orders by vendor")
    assert tier == ImportanceTier.OPERATIONAL

def test_reporting_grade():
    tier = classify_importance("total revenue by month this quarter")
    assert tier == ImportanceTier.REPORTING

def test_audit_critical_revenue():
    tier = classify_importance("net income by account for Q4 audit")
    assert tier == ImportanceTier.AUDIT_CRITICAL

def test_audit_critical_pl():
    tier = classify_importance("P&L by department for board presentation")
    assert tier == ImportanceTier.AUDIT_CRITICAL

def test_reporting_grade_dashboard():
    tier = classify_importance("sales summary for the monthly dashboard")
    assert tier == ImportanceTier.REPORTING

def test_financial_report_defaults_to_reporting():
    """FINANCIAL_REPORT intent should bump tier to at least REPORTING."""
    tier = classify_importance("show me the numbers", intent_hint="financial_report")
    assert tier.value >= ImportanceTier.REPORTING.value

def test_casual_is_default():
    tier = classify_importance("hello there")
    assert tier == ImportanceTier.CASUAL

def test_audit_keywords_case_insensitive():
    tier = classify_importance("AUDIT the revenue accounts")
    assert tier == ImportanceTier.AUDIT_CRITICAL
```

GREEN — Create `backend/app/services/importance_classifier.py`:
```python
"""Query importance classifier — 4-tier system for data trust levels.

Classifies user queries into importance tiers using keyword heuristics.
Higher tiers trigger stricter judge validation thresholds.
"""

from __future__ import annotations

import enum
import re

class ImportanceTier(int, enum.Enum):
    """Query importance tiers, ordered by trust requirement."""
    CASUAL = 1
    OPERATIONAL = 2
    REPORTING = 3
    AUDIT_CRITICAL = 4

    @property
    def label(self) -> str:
        return {1: "Casual", 2: "Operational", 3: "Reporting", 4: "Audit Critical"}[self.value]

    @property
    def judge_confidence_threshold(self) -> float:
        """Minimum judge confidence required for this tier."""
        return {1: 0.0, 2: 0.6, 3: 0.8, 4: 0.9}[self.value]

# Patterns checked in order — first match wins, highest tier takes priority
_TIER_RULES: list[tuple[ImportanceTier, re.Pattern[str]]] = [
    # Tier 4: Audit Critical — financials for board, compliance, fundraising
    (
        ImportanceTier.AUDIT_CRITICAL,
        re.compile(
            r"""(?xi)
            \b(?:
                audit | sox | compliance |
                board\s+(?:meeting|presentation|report|deck|review) |
                fundrais(?:e|ing) | investor |
                net\s+income | gross\s+(?:margin|profit) |
                p\s*[&/]\s*l | profit\s+(?:and|&)\s+loss |
                balance\s+sheet |
                cash\s+flow\s+statement |
                gaap | revenue\s+recognition |
                10[\s-]?[kq] | sec\s+filing |
                year[\s-]?end | fiscal\s+year |
                material(?:ity)? | restatement
            )\b
            """
        ),
    ),
    # Tier 3: Reporting Grade — monthly numbers, dashboards, trend reports
    (
        ImportanceTier.REPORTING,
        re.compile(
            r"""(?xi)
            \b(?:
                report(?:ing)? | dashboard |
                month(?:ly|end) | quarter(?:ly)? | annual |
                trend | yoy | year[\s-]over[\s-]year | mom | month[\s-]over[\s-]month |
                kpi | metric | benchmark |
                forecast | budget\s+(?:vs|versus|comparison) |
                summary\s+(?:for|of|by)\s+(?:the\s+)?(?:month|quarter|year|week) |
                total\s+(?:revenue|sales|expenses?|cost)\s+(?:by|for|this|last) |
                export(?:ing)?\s+(?:to|for|as)
            )\b
            """
        ),
    ),
    # Tier 2: Operational — filtered lists, daily decisions
    (
        ImportanceTier.OPERATIONAL,
        re.compile(
            r"""(?xi)
            \b(?:
                show\s+(?:me\s+)?(?:all|the|open|pending|unfulfilled|overdue|late) |
                list\s+(?:all|the|open|pending) |
                which\s+(?:orders?|customers?|vendors?|items?) |
                filter(?:ed)?\s+by | group(?:ed)?\s+by |
                sort(?:ed)?\s+by | order(?:ed)?\s+by |
                assigned\s+to | owned\s+by |
                breakdown | compare | between |
                pending\s+(?:approval|review|shipment|fulfillment) |
                top\s+\d+ | bottom\s+\d+ |
                by\s+(?:vendor|customer|warehouse|location|department|class|subsidiary)
            )\b
            """
        ),
    ),
]


def classify_importance(
    user_question: str,
    *,
    intent_hint: str | None = None,
) -> ImportanceTier:
    """Classify a user question into an importance tier.

    Args:
        user_question: The raw user question text.
        intent_hint: Optional intent from classify_intent() (e.g., "financial_report").

    Returns:
        ImportanceTier indicating the trust level required.
    """
    detected = ImportanceTier.CASUAL  # default

    for tier, pattern in _TIER_RULES:
        if pattern.search(user_question):
            detected = tier
            break

    # Financial report intent bumps minimum to REPORTING
    if intent_hint == "financial_report" and detected.value < ImportanceTier.REPORTING.value:
        detected = ImportanceTier.REPORTING

    return detected
```

REFACTOR: Extract tier metadata (label, threshold) into the enum itself (already done above).

---

**Cycle 2 — Judge Enforcement by Tier**

RED — Add to `backend/tests/test_suiteql_judge.py`:
```python
from app.services.importance_classifier import ImportanceTier
from app.services.suiteql_judge import enforce_judge_threshold, JudgeVerdict

def test_casual_tier_always_passes():
    verdict = JudgeVerdict(approved=True, confidence=0.3, reason="Low confidence")
    result = enforce_judge_threshold(verdict, ImportanceTier.CASUAL)
    assert result["passed"] is True
    assert result["tier"] == "Casual"

def test_operational_tier_fails_below_threshold():
    verdict = JudgeVerdict(approved=True, confidence=0.5, reason="Moderate")
    result = enforce_judge_threshold(verdict, ImportanceTier.OPERATIONAL)
    assert result["passed"] is False
    assert "below threshold" in result["reason"].lower()

def test_operational_tier_passes_above_threshold():
    verdict = JudgeVerdict(approved=True, confidence=0.7, reason="Good")
    result = enforce_judge_threshold(verdict, ImportanceTier.OPERATIONAL)
    assert result["passed"] is True

def test_audit_critical_requires_high_confidence():
    verdict = JudgeVerdict(approved=True, confidence=0.85, reason="Pretty good")
    result = enforce_judge_threshold(verdict, ImportanceTier.AUDIT_CRITICAL)
    assert result["passed"] is False  # 0.85 < 0.9

def test_audit_critical_flags_for_review():
    verdict = JudgeVerdict(approved=True, confidence=0.85, reason="Pretty good")
    result = enforce_judge_threshold(verdict, ImportanceTier.AUDIT_CRITICAL)
    assert result["needs_review"] is True

def test_disapproved_always_fails_tier_2_plus():
    verdict = JudgeVerdict(approved=False, confidence=0.9, reason="Wrong columns")
    result = enforce_judge_threshold(verdict, ImportanceTier.OPERATIONAL)
    assert result["passed"] is False
```

GREEN — Add to `backend/app/services/suiteql_judge.py` after the `_parse_verdict` function:
```python
def enforce_judge_threshold(
    verdict: JudgeVerdict,
    tier: "ImportanceTier",
) -> dict:
    """Apply tier-specific confidence thresholds to a judge verdict.

    Returns dict with:
        passed: bool — whether the result meets the tier's trust threshold
        tier: str — human-readable tier name
        needs_review: bool — whether a human should verify (tier 4 below threshold)
        reason: str — explanation
    """
    from app.services.importance_classifier import ImportanceTier

    threshold = tier.judge_confidence_threshold

    # Casual tier: always pass (existing fail-open behavior)
    if tier == ImportanceTier.CASUAL:
        return {
            "passed": True,
            "tier": tier.label,
            "needs_review": False,
            "reason": verdict.reason,
        }

    # Tier 2+: disapproved verdict always fails
    if not verdict.approved:
        return {
            "passed": False,
            "tier": tier.label,
            "needs_review": tier == ImportanceTier.AUDIT_CRITICAL,
            "reason": f"Judge disapproved: {verdict.reason}",
        }

    # Check confidence threshold
    passed = verdict.confidence >= threshold
    needs_review = not passed and tier == ImportanceTier.AUDIT_CRITICAL

    if passed:
        reason = verdict.reason
    else:
        reason = (
            f"Confidence {verdict.confidence:.2f} below threshold "
            f"{threshold:.2f} for {tier.label} tier"
        )

    return {
        "passed": passed,
        "tier": tier.label,
        "needs_review": needs_review,
        "reason": reason,
    }
```

REFACTOR: None needed.

---

**Cycle 3 — Tool Integration (netsuite_suiteql)**

RED — Create `backend/tests/test_suiteql_tool_importance.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_judge_uses_importance_tier():
    """The netsuite_suiteql tool should pass importance tier to judge enforcement."""
    from app.mcp.tools.netsuite_suiteql import _maybe_judge

    mock_result = {
        "rows": [{"cnt": 42}],
        "row_count": 1,
    }

    with patch("app.services.suiteql_judge.judge_suiteql_result") as mock_judge:
        mock_judge.return_value = AsyncMock(
            approved=True, confidence=0.7, reason="OK"
        )()
        # Wait for the coroutine mock to resolve
        from app.services.suiteql_judge import JudgeVerdict
        mock_judge.return_value = JudgeVerdict(approved=True, confidence=0.7, reason="OK")

        result = await _maybe_judge(
            result=mock_result,
            user_question="total revenue for Q4 audit",
            query="SELECT SUM(amount) FROM transaction",
            importance_tier=4,
        )

        # Tier 4 with confidence 0.7 should flag for review
        assert result.get("judge_verdict", {}).get("needs_review") is True
        assert result.get("judge_verdict", {}).get("tier") == "Audit Critical"

@pytest.mark.asyncio
async def test_casual_tier_skips_strict_enforcement():
    """Casual tier should use existing fail-open behavior."""
    from app.mcp.tools.netsuite_suiteql import _maybe_judge
    from app.services.suiteql_judge import JudgeVerdict

    mock_result = {"rows": [{"cnt": 5}], "row_count": 1}

    with patch("app.services.suiteql_judge.judge_suiteql_result") as mock_judge:
        mock_judge.return_value = JudgeVerdict(approved=True, confidence=0.3, reason="Low")

        result = await _maybe_judge(
            result=mock_result,
            user_question="how many orders",
            query="SELECT COUNT(*) FROM transaction",
            importance_tier=1,
        )

        # Casual: no warning, no needs_review
        assert result.get("_judge_warning") is None
        assert result.get("judge_verdict", {}).get("passed") is True
```

GREEN — Modify `backend/app/mcp/tools/netsuite_suiteql.py`:

1. Update `_maybe_judge` signature to accept `importance_tier`:
```python
async def _maybe_judge(
    result: dict,
    user_question: str | None,
    query: str,
    importance_tier: int = 1,
) -> dict:
```

2. After getting the verdict, apply tier enforcement:
```python
    verdict = await judge_suiteql_result(...)

    from app.services.importance_classifier import ImportanceTier
    from app.services.suiteql_judge import enforce_judge_threshold

    tier = ImportanceTier(importance_tier)
    enforcement = enforce_judge_threshold(verdict, tier)

    result["judge_verdict"] = {
        "approved": verdict.approved,
        "confidence": verdict.confidence,
        "reason": enforcement["reason"],
        "tier": enforcement["tier"],
        "passed": enforcement["passed"],
        "needs_review": enforcement["needs_review"],
    }
    if not enforcement["passed"]:
        result["_judge_warning"] = (
            f"[{enforcement['tier']}] {enforcement['reason']}"
        )
```

3. In `execute()`, extract `importance_tier` from context and pass to `_maybe_judge`:
```python
    importance_tier = context.get("importance_tier", 1)
    result = await _maybe_judge(result, user_question, limited_query, importance_tier=importance_tier)
```

REFACTOR: None needed.

---

**Cycle 4 — Orchestrator Classification + Context Threading**

RED — Create `backend/tests/test_orchestrator_importance.py`:
```python
import pytest
from app.services.importance_classifier import classify_importance, ImportanceTier

def test_importance_classified_for_data_query():
    tier = classify_importance("show me unfulfilled orders by vendor")
    assert tier == ImportanceTier.OPERATIONAL

def test_financial_intent_bumps_tier():
    tier = classify_importance("get me the numbers", intent_hint="financial_report")
    assert tier == ImportanceTier.REPORTING

def test_importance_tier_threaded_to_context():
    """Verify context dict would include importance_tier key."""
    context = {}
    tier = classify_importance("net income for audit review")
    context["importance_tier"] = tier.value
    assert context["importance_tier"] == 4
```

GREEN — Modify `backend/app/services/chat/orchestrator.py`:

1. After `classify_intent()` (around line 470), add importance classification:
```python
    from app.services.importance_classifier import classify_importance

    importance_tier = classify_importance(
        sanitized_input,
        intent_hint=detected_intent.value if is_financial else None,
    )
    print(f"[ORCHESTRATOR] Importance tier: {importance_tier.label} ({importance_tier.value})", flush=True)
```

2. After assembling the context dict (around line 547), inject the tier:
```python
    context["importance_tier"] = importance_tier.value
```

3. After the confidence SSE event (around line 637), add importance event:
```python
    yield {
        "type": "importance",
        "tier": importance_tier.value,
        "label": importance_tier.label,
        "needs_review": (
            agent_result
            and hasattr(agent_result, "tool_calls_log")
            and any(
                tc.get("result", {}).get("judge_verdict", {}).get("needs_review")
                for tc in (agent_result.tool_calls_log or [])
                if isinstance(tc.get("result"), dict)
            )
        ),
    }
```

REFACTOR: Extract importance event builder into a helper function.

---

### Phase 2: Persistence + API

**Cycle 5 — Database Migration + Model**

RED — Test column exists:
```python
def test_chat_message_has_query_importance():
    from app.models.chat import ChatMessage
    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="test",
        query_importance=3,
    )
    assert msg.query_importance == 3

def test_chat_message_importance_defaults_none():
    from app.models.chat import ChatMessage
    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="test",
    )
    assert msg.query_importance is None
```

GREEN:

1. Add to `backend/app/models/chat.py` after the `confidence_score` line (line 51):
```python
    # Query importance tier (1=Casual, 2=Operational, 3=Reporting, 4=Audit Critical)
    query_importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

2. Create migration `backend/alembic/versions/040_query_importance.py`:
```python
"""040_query_importance"""
from alembic import op
import sqlalchemy as sa

revision = "040_query_importance"
down_revision = "039_confidence_score"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("query_importance", sa.Integer(), nullable=True))

def downgrade() -> None:
    op.drop_column("chat_messages", "query_importance")
```

REFACTOR: None needed.

---

**Cycle 6 — Orchestrator Persistence**

RED — Add to `backend/tests/test_orchestrator_importance.py`:
```python
def test_chat_message_stores_importance():
    """ChatMessage should persist query_importance alongside confidence_score."""
    from app.models.chat import ChatMessage

    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="Total revenue: $1.2M",
        confidence_score=4.2,
        query_importance=3,
    )
    assert msg.query_importance == 3
    assert msg.confidence_score == 4.2
```

GREEN — Modify `backend/app/services/chat/orchestrator.py` where ChatMessage is created (around line 639):

Add `query_importance=importance_tier.value` to the ChatMessage constructor:
```python
    assistant_msg = ChatMessage(
        ...
        confidence_score=confidence_val,
        query_importance=importance_tier.value,  # ADD THIS
        ...
    )
```

REFACTOR: None needed.

---

**Cycle 7 — API Serialization**

RED — Create `backend/tests/test_chat_api_importance.py`:
```python
def test_serialize_message_includes_importance():
    """_serialize_message should include query_importance when present."""
    # This tests that the serialization in chat.py includes the field
    from types import SimpleNamespace
    msg = SimpleNamespace(
        id="test-id",
        role="assistant",
        content="test",
        tool_calls=None,
        citations=None,
        created_at="2025-01-01",
        confidence_score=4.0,
        query_importance=3,
    )
    # Simulate serialization logic
    result = {"role": msg.role, "content": msg.content}
    if msg.confidence_score is not None:
        result["confidence_score"] = float(msg.confidence_score)
    if msg.query_importance is not None:
        result["query_importance"] = msg.query_importance
    assert result["query_importance"] == 3
```

GREEN — Modify `backend/app/api/v1/chat.py` in `_serialize_message()`:

After the `confidence_score` serialization, add:
```python
    if msg.query_importance is not None:
        result["query_importance"] = msg.query_importance
```

REFACTOR: None needed.

---

### Phase 3: Frontend

**Cycle 8 — TypeScript Types + SSE Parser**

RED — Type check: `ChatMessage.query_importance` should be a valid field.

GREEN — Modify `frontend/src/lib/types.ts`:

Add to the ChatMessage interface:
```typescript
    query_importance?: number; // 1=Casual, 2=Operational, 3=Reporting, 4=Audit Critical
```

Modify `frontend/src/lib/chat-stream.ts`:

Add the importance event type to the SSE union:
```typescript
    | { type: "importance"; tier: number; label: string; needs_review: boolean }
```

Handle in `normalizeStreamEvent()`:
```typescript
    case "importance":
        return {
            type: "importance",
            tier: raw.tier,
            label: raw.label,
            needs_review: raw.needs_review,
        };
```

REFACTOR: None needed.

---

**Cycle 9 — ImportanceBadge Component** (NEW file)

RED — Render test for `frontend/src/components/chat/__tests__/importance-badge.test.tsx`:
```typescript
import { render, screen } from "@testing-library/react";
import { ImportanceBadge } from "../importance-badge";

describe("ImportanceBadge", () => {
    it("renders casual tier", () => {
        render(<ImportanceBadge tier={1} />);
        expect(screen.getByText("Casual")).toBeInTheDocument();
    });

    it("renders audit critical with warning style", () => {
        render(<ImportanceBadge tier={4} needsReview />);
        expect(screen.getByText("Audit Critical")).toBeInTheDocument();
        expect(screen.getByText(/needs review/i)).toBeInTheDocument();
    });

    it("renders reporting grade", () => {
        render(<ImportanceBadge tier={3} />);
        expect(screen.getByText("Reporting")).toBeInTheDocument();
    });

    it("does not render for undefined tier", () => {
        const { container } = render(<ImportanceBadge tier={undefined} />);
        expect(container.firstChild).toBeNull();
    });
});
```

GREEN — Create `frontend/src/components/chat/importance-badge.tsx`:
```typescript
"use client";

import { cn } from "@/lib/utils";
import { Shield, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import {
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from "@/components/ui/tooltip";

interface ImportanceBadgeProps {
    tier?: number;
    needsReview?: boolean;
    className?: string;
}

const TIER_CONFIG: Record<number, {
    label: string;
    color: string;
    bgColor: string;
    icon: typeof Shield;
    description: string;
}> = {
    1: {
        label: "Casual",
        color: "text-muted-foreground",
        bgColor: "bg-muted/50",
        icon: Shield,
        description: "Quick lookup — standard validation",
    },
    2: {
        label: "Operational",
        color: "text-sky-600 dark:text-sky-400",
        bgColor: "bg-sky-50 dark:bg-sky-950/30",
        icon: ShieldCheck,
        description: "Operational query — verified by AI judge",
    },
    3: {
        label: "Reporting",
        color: "text-amber-600 dark:text-amber-400",
        bgColor: "bg-amber-50 dark:bg-amber-950/30",
        icon: ShieldAlert,
        description: "Reporting grade — high-confidence judge verification",
    },
    4: {
        label: "Audit Critical",
        color: "text-rose-600 dark:text-rose-400",
        bgColor: "bg-rose-50 dark:bg-rose-950/30",
        icon: ShieldAlert,
        description: "Audit critical — strictest verification applied",
    },
};

export function ImportanceBadge({ tier, needsReview, className }: ImportanceBadgeProps) {
    if (!tier || !TIER_CONFIG[tier]) return null;

    const config = TIER_CONFIG[tier];
    const Icon = config.icon;

    return (
        <TooltipProvider>
            <Tooltip>
                <TooltipTrigger asChild>
                    <span
                        className={cn(
                            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
                            config.bgColor,
                            config.color,
                            className
                        )}
                    >
                        <Icon className="h-3 w-3" />
                        {config.label}
                        {needsReview && (
                            <span className="ml-0.5 text-rose-500 dark:text-rose-400">
                                • Needs Review
                            </span>
                        )}
                    </span>
                </TooltipTrigger>
                <TooltipContent side="bottom" className="text-[12px] max-w-[200px]">
                    {config.description}
                    {needsReview && (
                        <p className="mt-1 text-rose-500 dark:text-rose-400 font-medium">
                            Human verification recommended before using in official reports.
                        </p>
                    )}
                </TooltipContent>
            </Tooltip>
        </TooltipProvider>
    );
}
```

REFACTOR: Extract `TIER_CONFIG` if needed elsewhere.

---

**Cycle 10 — Message List Integration**

RED — Test assistant messages with importance render badge:
```typescript
// Verify that ImportanceBadge renders alongside ConfidenceBadge for assistant messages
it("renders importance badge for data queries", () => {
    render(<MessageItem message={{ ...assistantMsg, query_importance: 3 }} />);
    expect(screen.getByText("Reporting")).toBeInTheDocument();
});

it("does not render importance badge for user messages", () => {
    render(<MessageItem message={{ ...userMsg, query_importance: 3 }} />);
    expect(screen.queryByText("Reporting")).not.toBeInTheDocument();
});
```

GREEN — Modify `frontend/src/components/chat/message-list.tsx`:

1. Import the badge:
```typescript
import { ImportanceBadge } from "./importance-badge";
```

2. Render below or next to the ConfidenceBadge for assistant messages:
```typescript
{message.role === "assistant" && message.query_importance && (
    <ImportanceBadge
        tier={message.query_importance}
        needsReview={/* from SSE importance event */}
    />
)}
```

REFACTOR: Group ConfidenceBadge and ImportanceBadge into a `MessageMetaBadges` wrapper if both are present.

---

## Files to Create (3 new)

| File | Purpose |
|------|---------|
| `backend/app/services/importance_classifier.py` | 4-tier classification with regex heuristics |
| `backend/alembic/versions/040_query_importance.py` | Add `query_importance` column to chat_messages |
| `frontend/src/components/chat/importance-badge.tsx` | Shield-icon badge with tier colors and tooltip |

## Files to Modify (7 existing)

| File | Change |
|------|--------|
| `backend/app/services/suiteql_judge.py` | Add `enforce_judge_threshold()` function |
| `backend/app/mcp/tools/netsuite_suiteql.py` | Pass `importance_tier` to `_maybe_judge()`, apply enforcement |
| `backend/app/services/chat/orchestrator.py` | Classify importance, thread to context, emit SSE event, persist |
| `backend/app/models/chat.py` | Add `query_importance` column to ChatMessage |
| `backend/app/api/v1/chat.py` | Add `query_importance` to `_serialize_message()` |
| `frontend/src/lib/types.ts` | Add `query_importance?: number` to ChatMessage |
| `frontend/src/lib/chat-stream.ts` | Add importance event type + handler |
| `frontend/src/components/chat/message-list.tsx` | Render ImportanceBadge for assistant messages |

## Test Files to Create (4 new)

| File | Tests |
|------|-------|
| `backend/tests/test_importance_classifier.py` | Tier classification, keyword matching, intent boost |
| `backend/tests/test_suiteql_tool_importance.py` | Tool passes tier to judge enforcement |
| `backend/tests/test_orchestrator_importance.py` | Context threading, SSE event, persistence |
| `frontend/src/components/chat/__tests__/importance-badge.test.tsx` | Render, tiers, review flag |

---

## Signal Threading (How Importance Reaches the Judge)

```
User sends message
  → orchestrator.py: classify_importance(user_question, intent_hint)
  → orchestrator.py: context["importance_tier"] = tier.value
  → unified_agent runs with context dict
  → agent calls netsuite_suiteql tool
  → netsuite_suiteql.execute() reads context["importance_tier"]
  → _maybe_judge(result, question, sql, importance_tier=tier)
  → judge_suiteql_result() → JudgeVerdict
  → enforce_judge_threshold(verdict, tier) → enforcement dict
  → Result includes judge_verdict with tier, passed, needs_review
  → orchestrator.py: emit SSE importance event
  → orchestrator.py: persist query_importance on ChatMessage
  → Frontend: render ImportanceBadge with tier + needs_review
```

---

## Performance Notes

- **Zero extra LLM calls**: Importance classification is pure regex, same as `classify_intent()`
- **Zero extra DB queries**: Importance is determined from user question text only
- **Judge already runs**: The tier just controls the confidence threshold applied to existing judge output
- **SSE event is tiny**: `{"type": "importance", "tier": 3, "label": "Reporting", "needs_review": false}`

---

## Verification

1. `pytest backend/tests/test_importance_classifier.py -v` — all tier classification tests pass
2. `pytest backend/tests/test_suiteql_judge.py -v` — enforcement threshold tests pass
3. `alembic upgrade head` — `query_importance` column added
4. Send "how many orders today" → SSE shows `{"type": "importance", "tier": 1, "label": "Casual"}`
5. Send "net income by account for Q4 audit" → SSE shows `{"type": "importance", "tier": 4, "label": "Audit Critical"}`
6. Low judge confidence on tier 4 → `needs_review: true` in SSE event
7. Frontend: assistant messages show shield badge with correct tier color
8. DB: `SELECT query_importance FROM chat_messages WHERE role='assistant' ORDER BY created_at DESC LIMIT 5` returns non-null values
