"""Test the financial-ambiguity detector regex + connector-gate logic."""

import pytest

from app.services.chat.plan_mode.ambiguity_signal import is_financial_ambiguous


@pytest.mark.parametrize(
    "query",
    [
        "What's our revenue this quarter?",
        "How's our gross margin tracking?",
        "Show me MRR for last 3 months",
        "Top line for Q2",
        "GMV breakdown by month",
        "EBITDA YTD",
        "Net income last fiscal year",
        "What was bookings vs ARR?",
        "Operating income trend",
        "Recognized revenue in May",
        "Earnings this quarter",
        "Cogs as % of net sales",
    ],
)
def test_detects_financial_ambiguity(query):
    assert is_financial_ambiguous(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "How many sales orders today?",
        "Top 10 customers by item count",
        "Inventory turnover for SKU A100",
        "When was order SO12345 fulfilled?",
        "Show me customer addresses",
        "RMA volume last month",
    ],
)
def test_does_not_detect_non_financial(query):
    assert is_financial_ambiguous(query) is False


def test_case_insensitive():
    assert is_financial_ambiguous("REVENUE this quarter") is True
    assert is_financial_ambiguous("revenue") is True


def test_word_boundary():
    """'revenuecycle' or 'gmv-something' shouldn't match — only standalone words."""
    assert is_financial_ambiguous("revenuecycle department") is False


def test_empty_string():
    assert is_financial_ambiguous("") is False


def test_none_safe():
    """Defensive: None-safe (orchestrator may pass None for blank turns)."""
    assert is_financial_ambiguous(None) is False  # type: ignore[arg-type]


from app.services.chat.plan_mode.ambiguity_signal import build_augmentation_prompt


def test_augmentation_includes_clarify_directive():
    prompt = build_augmentation_prompt()
    assert "CLARIFICATION REQUIRED" in prompt
    assert "clarify" in prompt
    assert "MUST" in prompt or "ONLY" in prompt
    assert "data tool" in prompt.lower() or "data tools" in prompt.lower()


def test_augmentation_includes_default_preferences():
    prompt = build_augmentation_prompt()
    assert "NetSuite GL" in prompt
    assert "BigQuery" in prompt


def test_augmentation_mentions_ambiguity_axes():
    prompt = build_augmentation_prompt()
    assert "source" in prompt.lower()
    assert "window" in prompt.lower() or "fiscal" in prompt.lower()
    assert "scope" in prompt.lower() or "subsidiary" in prompt.lower() or "consolidated" in prompt.lower()


def test_augmentation_mentions_default_explanation_directive():
    """Plan-eng-review locked this: the agent must explain WHY the default."""
    prompt = build_augmentation_prompt()
    assert "ambiguity_summary" in prompt
    assert "default" in prompt.lower()


def test_augmentation_is_stable():
    """Pure function — same call twice returns identical output."""
    assert build_augmentation_prompt() == build_augmentation_prompt()


def test_augmentation_lists_connected_sources_when_provided():
    """When ≥2 sources connected, prompt must name each by canonical name so the
    model picks across them instead of slicing within one source.
    """
    prompt = build_augmentation_prompt(connected_sources=["netsuite", "bigquery", "stripe"])
    assert "netsuite" in prompt.lower()
    assert "bigquery" in prompt.lower()
    assert "stripe" in prompt.lower()


def test_augmentation_requires_source_spanning_when_multi_source():
    """With ≥2 connected sources, the model must spread options across distinct
    sources. Either via the generic source-spanning rule OR — when NetSuite is
    one of the connected sources — via the more specific NS-split rule.
    """
    prompt = build_augmentation_prompt(connected_sources=["netsuite", "bigquery"])
    lower = prompt.lower()
    # Generic spanning OR NS-split rule satisfies "options span sources"
    assert "distinct" in lower or "different source" in lower or "span" in lower or "another connected source" in lower
    # Both rule variants forbid collapsing to a single source slice
    assert (
        "same source" in lower
        or "one source" in lower
        or "within-source" in lower
        or "two options must come from netsuite" in lower
    )


def test_augmentation_allows_within_source_when_single_source():
    """With only one connected source, source-spanning is impossible — the
    prompt should permit within-source variation (window/scope/metric definition).
    """
    prompt = build_augmentation_prompt(connected_sources=["netsuite"])
    lower = prompt.lower()
    assert "window" in lower or "scope" in lower or "fiscal" in lower or "metric" in lower


def test_augmentation_falls_back_to_static_when_no_sources_passed():
    """Backwards compat: callers that omit connected_sources still get a usable
    prompt (used by tests and any legacy call sites).
    """
    prompt = build_augmentation_prompt()
    assert "CLARIFICATION REQUIRED" in prompt


def test_augmentation_requires_netsuite_split_when_netsuite_plus_other():
    """When NetSuite is connected alongside ≥1 other canonical source, the
    prompt must require BOTH NetSuite recognized revenue AND NetSuite booked
    sales orders as separate options, plus one cross-source option.

    Dogfood feedback (Framework, 2026-04-30): the recognized-vs-booked
    distinction is materially different (bookings ≠ revenue) and must be
    preserved alongside source diversity.
    """
    prompt = build_augmentation_prompt(connected_sources=["netsuite", "bigquery", "stripe"])
    lower = prompt.lower()
    assert "recognized revenue" in lower or "posted invoices" in lower
    assert "booked" in lower and ("sales order" in lower or "sos" in lower)
    # Must explicitly call out the slot allocation when NS + other sources
    assert "two netsuite" in lower or "two options" in lower or "both netsuite" in lower


def test_augmentation_skips_netsuite_split_when_only_netsuite():
    """When NetSuite is the only connected source, source-spanning is
    impossible. Within-source variation is allowed (window/scope/metric)
    and the NS-split rule should not fire.
    """
    prompt = build_augmentation_prompt(connected_sources=["netsuite"])
    lower = prompt.lower()
    # The within-source variation rule should still apply
    assert "window" in lower or "scope" in lower or "metric" in lower


from app.services.chat.plan_mode.ambiguity_signal import maybe_augment_for_plan_mode


def test_helper_returns_block_when_flag_on_and_ambiguous():
    block = maybe_augment_for_plan_mode(query="What's our revenue this quarter?", plan_mode_enabled=True)
    assert block is not None
    assert "CLARIFICATION REQUIRED" in block


def test_helper_returns_none_when_flag_off():
    assert maybe_augment_for_plan_mode(query="What's our revenue this quarter?", plan_mode_enabled=False) is None


def test_helper_returns_none_when_query_not_ambiguous():
    assert maybe_augment_for_plan_mode(query="How many sales orders today?", plan_mode_enabled=True) is None


def test_helper_handles_empty_query():
    assert maybe_augment_for_plan_mode(query="", plan_mode_enabled=True) is None
    assert maybe_augment_for_plan_mode(query=None, plan_mode_enabled=True) is None  # type: ignore[arg-type]


def test_helper_forwards_connected_sources():
    """Helper must thread connected_sources through to build_augmentation_prompt."""
    block = maybe_augment_for_plan_mode(
        query="What's our revenue this quarter?",
        plan_mode_enabled=True,
        connected_sources=["netsuite", "bigquery"],
    )
    assert block is not None
    assert "netsuite" in block.lower()
    assert "bigquery" in block.lower()
