"""Financial-ambiguity detector + system-prompt augmentation hook (Component 1)."""

import logging
import re

_logger = logging.getLogger(__name__)

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


def maybe_augment_for_plan_mode(*, query: str | None, plan_mode_enabled: bool) -> str | None:
    """Return the Plan Mode augmentation block when both gates pass, else None.

    Caller (orchestrator.run_chat_turn) appends the returned block to the
    system prompt right after the source-pin hint, so Plan Mode overrides any
    pinned source for financial-ambiguous turns.
    """
    if not plan_mode_enabled:
        return None
    if not is_financial_ambiguous(query):
        return None
    return build_augmentation_prompt()


def filter_tools_to_clarify_only(tools: list[dict]) -> list[dict]:
    """Return only the `clarify` tool from the inventory.

    Used on financial-ambiguous turns to make data-tool calls impossible at
    the schema level — the model literally cannot emit a tool_use for any
    other tool. Returns an empty list if `clarify` is absent (caller must
    check and skip gate activation).
    """
    return [t for t in tools if t.get("name") == "clarify"]


def try_force_tool_choice(adapter, tool_name: str, model: str | None = None) -> dict | None:
    """Wrap adapter.force_tool_choice; return None on PlanModeUnsupportedError.

    Caller treats None as "skip Plan Mode for this turn" (graceful degradation
    on adapters that can't enforce tool_choice — e.g., Gemini 1.0).
    """
    from app.services.chat.plan_mode.errors import PlanModeUnsupportedError

    try:
        if model is not None:
            return adapter.force_tool_choice(tool_name, model=model)
        return adapter.force_tool_choice(tool_name)
    except PlanModeUnsupportedError as e:
        _logger.warning(
            "plan_mode_unsupported provider=%s reason=%s — skipping Plan Mode for turn",
            e.provider,
            e.reason,
        )
        return None
