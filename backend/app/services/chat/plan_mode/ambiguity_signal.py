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


_AUGMENTATION_PROMPT = """## CLARIFICATION REQUIRED

This query contains financial terminology that has multiple legitimate readings.
Sources of ambiguity may include: which data source (NetSuite GL recognized
revenue vs BigQuery checkout totals vs Shopify gross sales), which window (fiscal
vs calendar quarter), which scope (consolidated vs subsidiary), which metric
definition (booked vs paid, gross vs recognized).

Your ONLY allowed first action is a single `clarify` tool call. Build 2-3
plausible interpretation options grounded in the actual ambiguity axes for THIS
query. Mark one as default. Default preferences: NetSuite GL for "revenue" /
"income" / "earnings" / "recognized revenue"; BigQuery for "GMV" / "checkout" /
"online sales"; fiscal calendar for quarterly windows. Use only connected
sources.

In `ambiguity_summary`, write a one-sentence framing in your own voice that
NAMES THE DEFAULT REASON. Example: "I'm picking NetSuite GL by default because
that's recognized revenue — if you want pre-refund checkout dollars, B is right."

You MUST NOT call any data tool in the same turn as `clarify`. The user's
choice arrives on the next turn."""


def build_augmentation_prompt() -> str:
    """Return the system-prompt augmentation block for financial-ambiguous turns.

    Appended after the source-pin hint in `_assemble_system_prompt` so the
    augmentation overrides any pinned source for financial queries.
    """
    return _AUGMENTATION_PROMPT
