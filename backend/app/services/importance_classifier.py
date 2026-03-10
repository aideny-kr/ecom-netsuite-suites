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
