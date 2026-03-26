"""Classify follow-up messages as TRANSFORM (reuse data) or NEW_DATA (re-query)."""

import re
from enum import Enum


class FollowUpIntent(str, Enum):
    TRANSFORM = "TRANSFORM"
    NEW_DATA = "NEW_DATA"


# Tier 1: Regex — catches obvious transform requests (<1ms)
_TRANSFORM_RE = re.compile(
    r"\b(chart|graph|plot|visuali[sz]e|pivot|export|download|csv|excel)\b.*\b(th(?:is|at|ese|ose)|above|result|data|it|same)\b"
    r"|\b(th(?:is|at|ese|ose)|above|result|data|it)\b.*\b(chart|graph|plot|visuali[sz]e|pivot|export|csv|bar|line|pie)\b"
    r"|^(chart|graph|plot|pivot|export|visuali[sz]e)\s+(th|it|this|that)"
    r"|^(make|create|build|show|give me)\s+(a\s+)?(chart|graph|plot|bar|line|pie)"
    r"|\b(different|another)\s+(chart|view|format|visualization)"
    r"|^(sort|filter|group|rank|top\s+\d+|bottom\s+\d+)\s+(th|it|this|that|by|from)\b"
    r"|\bmake\s+it\s+a\s+(line|bar|pie|area|scatter)\s+chart\b",
    re.IGNORECASE,
)

# Negative lookahead: if the message also contains time/entity switches, it's NEW_DATA
_NEW_DATA_OVERRIDE_RE = re.compile(
    r"\b(but\s+for|switch\s+to|change\s+to|different\s+(?:period|quarter|month|year|date|customer|subsidiary))\b"
    r"|\binstead\s+of\s+(?:last|this|Q[1-4]|20\d{2}|the\s+(?:previous|prior)|a\s+(?:different|new))\b"
    r"|\b(last\s+(?:month|quarter|year)|this\s+(?:month|quarter|year)|Q[1-4]\b|20\d{2})\b",
    re.IGNORECASE,
)


def classify_follow_up(
    message: str,
    has_previous_result: bool,
) -> FollowUpIntent:
    """Classify a follow-up message. Only considers TRANSFORM when there IS a previous result."""
    if not has_previous_result:
        return FollowUpIntent.NEW_DATA

    # Check for NEW_DATA override first (entity/time switch)
    if _NEW_DATA_OVERRIDE_RE.search(message):
        return FollowUpIntent.NEW_DATA

    # Tier 1: Regex match for transform intent
    if _TRANSFORM_RE.search(message):
        return FollowUpIntent.TRANSFORM

    return FollowUpIntent.NEW_DATA
