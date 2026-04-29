"""Financial-ambiguity detector + system-prompt augmentation hook (Component 1)."""

import re

# Distinct from orchestrator._FINANCIAL_RE (which is narrower — used for
# context-need classification). This regex catches phrases that are AMBIGUOUS
# across data sources / windows / metric definitions.
_FINANCIAL_AMBIGUITY_RE = re.compile(
    r"\b(?:revenue|top\s*line|gmv|gross\s*(?:sales|margin)|net\s*(?:sales|margin|income)|"
    r"ebitda|cogs|operating\s*income|earnings|recognized\s*revenue|bookings|"
    r"mrr|arr|burn|runway)\b",
    re.IGNORECASE,
)


def is_financial_ambiguous(query: str | None) -> bool:
    """Return True if the query contains financial terminology that has
    multiple legitimate interpretations across sources/windows/scopes.

    NOTE: fires regardless of connector count (Codex review finding 2 —
    "revenue this quarter" is ambiguous even with one connector: gross vs
    recognized, fiscal vs calendar Q, booked vs paid).
    """
    if not query:
        return False
    return bool(_FINANCIAL_AMBIGUITY_RE.search(query))
