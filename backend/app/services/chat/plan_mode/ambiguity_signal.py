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


_AUGMENTATION_PREAMBLE = """## CLARIFICATION REQUIRED

This query contains financial terminology that has multiple legitimate readings.
The CFO-grade ambiguity is FIRST about which data source has the answer (numbers
differ materially between sources for the same metric: NetSuite GL recognized
revenue ≠ BigQuery checkout totals ≠ Shopify gross sales ≠ Stripe collected
cash), and only second about within-source axes (fiscal vs calendar window,
consolidated vs subsidiary scope, booked vs paid metric definition).

Your ONLY allowed first action is a single `clarify` tool call. You MUST NOT
call any data tool in the same turn. The user's choice arrives on the next
turn."""

_AUGMENTATION_TRAILER = """In `ambiguity_summary`, write a one-sentence framing in your own voice that
NAMES THE DEFAULT REASON. Example: "I'm picking NetSuite GL by default because
that's recognized revenue — if you want pre-refund checkout dollars, B is right."

Default preferences: NetSuite GL for "revenue" / "income" / "earnings" /
"recognized revenue"; BigQuery for "GMV" / "checkout" / "online sales"; fiscal
calendar for quarterly windows.

Metric-specific cross-source routing — when picking the cross-source slot,
match the metric to where its canonical data actually lives:
- "MRR" / "ARR" / "subscription revenue" / "recurring revenue" → Stripe is
  the natural cross-source (subscription billing data lives there). Do not
  drop the cross-source option for these queries — even if NetSuite has a
  recognized-revenue answer, the subscription-side number is materially
  different and worth surfacing.
- "GMV" / "checkout" / "online sales" / "ecommerce revenue" → BigQuery is
  the natural cross-source (order-level checkout data).
- "cash" / "payouts" / "collected" / "deposited" → Stripe is the natural
  cross-source (cash-in/processor-side gross)."""

# Human-readable labels for canonical sources so the rendered prompt explains
# what each source means rather than just naming it.
_SOURCE_LABELS: dict[str, str] = {
    "netsuite": "NetSuite (GL recognized revenue, posted invoices, sales orders)",
    "bigquery": "BigQuery (ecommerce checkout totals, web/app analytics)",
    "shopify": "Shopify (gross sales, refunds, online storefront)",
    "stripe": "Stripe (collected cash, payouts, processor-side gross)",
    "drive": "Google Drive (uploaded spreadsheets, finance docs)",
}


def _render_source_list(connected_sources: list[str]) -> str:
    seen: list[str] = []
    for src in connected_sources:
        if src not in seen and src in _SOURCE_LABELS:
            seen.append(src)
    if not seen:
        return ""
    bullets = "\n".join(f"- {src}: {_SOURCE_LABELS[src]}" for src in seen)
    return f"This tenant has these connected sources:\n{bullets}\n"


def build_augmentation_prompt(connected_sources: list[str] | None = None) -> str:
    """Return the system-prompt augmentation block for financial-ambiguous turns.

    When ``connected_sources`` lists ≥2 canonical sources, the prompt requires
    options to span distinct sources before falling back to within-source
    variation. With 0 or 1 sources, the prompt allows within-source variation
    (window / scope / metric definition).
    """
    sources = connected_sources or []
    rendered_sources = _render_source_list(sources)
    distinct_canonical = [s for s in sources if s in _SOURCE_LABELS]
    canonical_set = set(distinct_canonical)
    multi_source = len(canonical_set) >= 2
    netsuite_plus_other = "netsuite" in canonical_set and len(canonical_set) >= 2

    if netsuite_plus_other:
        # Dogfood feedback (Framework 2026-04-30): the recognized-vs-booked
        # distinction inside NetSuite is materially different (bookings ≠
        # revenue) and must be preserved alongside source diversity. Slot
        # allocation: A=NS recognized, B=NS booked SOs, C=cross-source.
        rule = (
            "RULE — slot allocation when NetSuite is connected with another "
            "source:\n"
            "- Two options MUST come from NetSuite, covering both materially "
            "different views:\n"
            "  - Recognized revenue (posted invoices / cash sales / GL — the "
            "GAAP accounting answer)\n"
            "  - Booked sales orders (SOs created this period regardless of "
            "fulfillment or invoicing — the bookings/pipeline answer)\n"
            "- One option MUST come from another connected source (BigQuery "
            "checkout totals for revenue/GMV/sales queries; Stripe collected "
            "cash for cash/payout queries).\n"
            "Bookings ≠ revenue — collapsing both into a single NetSuite "
            "option hides a material number CFOs care about."
        )
    elif multi_source:
        rule = (
            "RULE: Build 2-3 options that span DISTINCT sources. Each option's "
            "`source` field MUST be different until every connected source has "
            "at least one option. Do NOT pick multiple options from the same "
            "source while another connected source is unused — different "
            "sources give materially different numbers and that is the "
            "primary clarification axis. Within-source variation (window, "
            "scope, metric definition) is allowed only after every connected "
            "source already appears."
        )
    else:
        rule = (
            "Build 2-3 plausible interpretation options grounded in the "
            "actual ambiguity axes for THIS query (window, scope, metric "
            "definition). Use only the connected source(s)."
        )

    parts = [_AUGMENTATION_PREAMBLE]
    if rendered_sources:
        parts.append(rendered_sources)
    parts.append(rule)
    parts.append(_AUGMENTATION_TRAILER)
    return "\n\n".join(parts)


def maybe_augment_for_plan_mode(
    *,
    query: str | None,
    plan_mode_enabled: bool,
    connected_sources: list[str] | None = None,
) -> str | None:
    """Return the Plan Mode augmentation block when both gates pass, else None.

    Caller (orchestrator.run_chat_turn) appends the returned block to the
    system prompt right after the source-pin hint, so Plan Mode overrides any
    pinned source for financial-ambiguous turns. When ``connected_sources``
    is supplied, the prompt is rendered with source-spanning rules.
    """
    if not plan_mode_enabled:
        return None
    if not is_financial_ambiguous(query):
        return None
    return build_augmentation_prompt(connected_sources=connected_sources)


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
