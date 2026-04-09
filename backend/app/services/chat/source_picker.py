"""Source picker: confidence-gated router between NetSuite and BigQuery.

When a user's data question can be answered by both sources, the agent shows
a two-card picker and asks the user to choose. When one source is clearly
correct (confidence >= AMBIGUITY_THRESHOLD), the agent skips the picker and
runs directly.
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

Source = Literal["netsuite", "bigquery"]
SourceScore = tuple[Source, float, str]  # (recommended, confidence, reason)

AMBIGUITY_THRESHOLD = 0.85


# ── Signal regexes ─────────────────────────────────────────────────────────
# Financial statement vocabulary — 100% NetSuite (accounting source of truth).
_FINANCIAL_RE = re.compile(
    r"""(?xi)
    \b(?:
        income\s+statements? |
        profit\s*(?:&|and)\s*loss |
        p\s*[&/]\s*l\b |
        balance\s+sheets? |
        cash\s+flow\s+statements? |
        trial\s+balance |
        general\s+ledger |
        gl\s+(?:summary|report|balance|impact|entries|detail) |
        chart\s+of\s+accounts |
        ebitda |
        consolidated\s+(?:financials?|revenue|income) |
        financial\s+(?:statements?|reports?|summary) |
        fiscal\s+(?:year|period|quarter)\s+(?:report|summary|results?)
    )\b
    """
)

# NetSuite operational entities — 95%+ NetSuite.
_NETSUITE_ENTITY_RE = re.compile(
    r"""(?xi)
    \b(?:
        outstanding\s+balance |
        accounts?\s+receivable |
        a/?r\s+aging |
        open\s+(?:invoices?|orders?|bills?) |
        journal\s+entries? |
        credit\s+memos? |
        vendor\s+bills? |
        customer\s+balance
    )\b
    """
)

# Supply-chain / operational NetSuite-only entities.
_NETSUITE_OPERATIONAL_RE = re.compile(
    r"""(?xi)
    \b(?:
        inventory\s+(?:adjustments?|counts?|on\s+hand) |
        purchase\s+orders? |
        item\s+(?:receipts?|fulfillments?) |
        subsidiary |
        departments? |
        warehouse |
        bin\s+transfers?
    )\b
    """
)

# Marketing / BI-only vocabulary — 95%+ BigQuery.
_MARKETING_RE = re.compile(
    r"""(?xi)
    \b(?:
        ad\s+spend |
        attribution |
        channels? |
        campaigns? |
        cohorts? |
        conversion\s+rate |
        utm |
        roas |
        cac |
        ltv |
        acquisition |
        sessions? |
        page\s+views?
    )\b
    """
)

# Explicit user mentions of a source.
_EXPLICIT_BIGQUERY_RE = re.compile(r"(?i)\b(?:bigquery|bq|data\s+warehouse)\b")
_EXPLICIT_NETSUITE_RE = re.compile(r"(?i)\b(?:netsuite|net\s*suite|suiteql|ns\s+(?:records?|data))\b")

# Data-intent heuristic — picker only fires when the query looks like a data
# question. Chitchat ("hello"), workspace questions ("show me workspace files"),
# and meta questions ("how do I use this") must skip the picker.
_DATA_INTENT_RE = re.compile(
    r"""(?xi)
    \b(?:
        # Core data entity nouns
        orders? | invoices? | customers? | vendors? | suppliers? |
        transactions? | payments? | refunds? | charges? | deposits? |
        payouts? | sales? | revenue | revenues? | sku | skus |
        products? | items? | shipments? | fulfillments? | returns? |
        bills? | receipts? | quotes? | estimates? | opportunities? |
        subscriptions? | discounts? | coupons? | taxes? | margins? |
        # Aggregation verbs
        total | totals? | sum | average | mean | median | count |
        top\s+\d* | bottom\s+\d* | best\s+selling | worst |
        # Time-window data questions
        this\s+(?:week|month|quarter|year|day) |
        last\s+(?:week|month|quarter|year|day|\d+\s+(?:days?|weeks?|months?|quarters?|years?)) |
        ytd | mtd | qtd | wtd |
        q[1-4] | fy\d{2,4} |
        year[-\s]?over[-\s]?year | yoy | month[-\s]?over[-\s]?month | mom |
        # Report / trend nouns
        trend | trends | growth | decline | variance | forecast |
        dashboards? | reports? | metrics? | kpis?
    )\b
    """
)


def has_data_intent(query: str) -> bool:
    """Return True iff the query looks like a data question.

    Chitchat, greetings, workspace file questions, and meta questions about
    the app itself should not trigger the source picker. This is a coarse
    heuristic — false positives are acceptable (picker appears), false
    negatives are not (picker should not hijack a workspace or chitchat turn).
    """
    if not query or not query.strip():
        return False
    if _EXPLICIT_BIGQUERY_RE.search(query) or _EXPLICIT_NETSUITE_RE.search(query):
        return True
    if _FINANCIAL_RE.search(query):
        return True
    if _NETSUITE_ENTITY_RE.search(query) or _NETSUITE_OPERATIONAL_RE.search(query):
        return True
    if _MARKETING_RE.search(query):
        return True
    return bool(_DATA_INTENT_RE.search(query))


class PickerOption(TypedDict):
    source: Source
    label: str
    description: str
    recommended: bool


class PickerPayload(TypedDict):
    type: Literal["source_picker"]
    recommended: Source
    confidence: float
    reason: str
    user_question: str
    options: list[PickerOption]


def score_source(query: str) -> SourceScore:
    """Score a query to decide which source to use, with confidence.

    Returns (recommended_source, confidence 0..1, short human reason).

    Confidence bands:
    - 0.95+ : unambiguous (financial statements, marketing vocab, explicit mention)
    - 0.90  : strong signal (NetSuite entity, NetSuite operational)
    - 0.55  : ambiguous — both sources can answer; default to NetSuite as source of truth
    - 0.50  : unclear — default NetSuite
    """
    if not query or not query.strip():
        return ("netsuite", 0.50, "empty query, defaulting to source of truth")

    # Explicit mentions trump everything.
    if _EXPLICIT_BIGQUERY_RE.search(query):
        return ("bigquery", 0.99, "user explicitly mentioned BigQuery")
    if _EXPLICIT_NETSUITE_RE.search(query):
        return ("netsuite", 0.99, "user explicitly mentioned NetSuite")

    # Financial statements → NetSuite (unambiguous).
    if _FINANCIAL_RE.search(query):
        return ("netsuite", 0.99, "financial statement query")

    # NetSuite-specific entities → NetSuite.
    if _NETSUITE_ENTITY_RE.search(query):
        return ("netsuite", 0.95, "NetSuite-native entity (AR/invoices/balances)")

    if _NETSUITE_OPERATIONAL_RE.search(query):
        return ("netsuite", 0.95, "NetSuite operational record (PO/inventory/subsidiary)")

    # Marketing / BI-only vocabulary → BigQuery.
    if _MARKETING_RE.search(query):
        return ("bigquery", 0.95, "marketing / BI vocabulary")

    # Ambiguous — both can answer. NetSuite is the source of truth, so
    # recommend it but with low confidence so the picker surfaces.
    return ("netsuite", 0.55, "operational data available in both sources")


def should_prompt_user(score: SourceScore, threshold: float = AMBIGUITY_THRESHOLD) -> bool:
    """True iff confidence is strictly below the threshold."""
    return score[1] < threshold


def build_picker_payload(score: SourceScore, *, user_question: str) -> PickerPayload:
    """Build the SSE + structured_output payload for the frontend picker card."""
    recommended, confidence, reason = score
    ns_option: PickerOption = {
        "source": "netsuite",
        "label": "NetSuite",
        "description": "Source of truth for daily operations and finance.",
        "recommended": recommended == "netsuite",
    }
    bq_option: PickerOption = {
        "source": "bigquery",
        "label": "BigQuery",
        "description": "BI & analytics warehouse — fast for trends and large aggregations.",
        "recommended": recommended == "bigquery",
    }
    return {
        "type": "source_picker",
        "recommended": recommended,
        "confidence": round(confidence, 2),
        "reason": reason,
        "user_question": user_question,
        "options": [ns_option, bq_option],
    }
