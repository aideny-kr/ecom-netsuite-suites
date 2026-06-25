# backend/tests/services/chat/test_metric_tool_categorization.py
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS
from app.services.chat.tool_categories import _EXACT, categorize


def test_metric_compute_is_data_table():
    assert categorize("metric_compute") == "data_table"
    assert categorize("metric.compute") == "data_table"


def test_metric_resolve_is_not_intercepted():
    # resolve returns definitions the LLM must read; it must NOT be stripped.
    assert categorize("metric_resolve") == "other"


# Chat tools that deliberately do NOT return a tabular/financial result and so are
# intentionally left to categorize() -> "other" (no _intercept_tool_result branch).
# Every entry here is a conscious decision: metadata / connectivity / control-flow /
# definition-read tools whose results the LLM is MEANT to read verbatim.
#
# This set is the ONLY sanctioned escape hatch from the "every chat tool is
# categorized" invariant below. Adding a NEW chat tool that returns numbers WITHOUT
# registering it in tool_categories._EXACT (so its result is intercepted) requires a
# deliberate edit here — which is exactly the review checkpoint the invariant enforces.
_ALLOWED_OTHER_CHAT_TOOLS: frozenset[str] = frozenset(
    {
        "data.sample_table_read",  # ≤30-row preview shown raw; not a blessed number source
        "metric.resolve",  # returns DISPLAY-ONLY definitions the LLM must read
        "netsuite.connectivity",  # status probe, no data rows
        "netsuite.get_metadata",  # schema metadata the LLM reads
        "netsuite.refresh_metadata",  # control action, no data rows
        # report.compose is now categorized as "report" in tool_categories._EXACT
        # (its SSE result is intercepted as report_ready), so it is no longer an
        # allow-"other" tool — see test_report_tool_registration.py.
        "suitescript.sync",  # control action, no data rows
        "tenant.save_learned_rule",  # write-side control action, no data rows
        "workspace.run_validate",  # validator output the LLM reads
    }
)


def test_every_chat_tool_is_deliberately_categorized():
    """REAL invariant: every chat-exposed tool maps to a DELIBERATE category.

    For each tool in ALLOWED_CHAT_TOOLS, its underscore form must EITHER be a
    registered entry in tool_categories._EXACT (a non-'other' interception
    category) OR appear in the explicit allow-'other' set above.

    Why this matters (the anti-hallucination core): _intercept_tool_result
    dispatches purely on categorize(tool_name). A tool that is NOT in _EXACT
    returns 'other', every interception branch is skipped, and the FULL result
    string (the raw number) is handed to the LLM — the exact leak the metric
    catalog exists to prevent. A future chat tool that returns numbers but is
    never categorized would silently bypass the trust boundary.

    This test makes that failure LOUD: a new uncategorized chat tool fails CI
    until someone either registers it in _EXACT or consciously allow-lists it
    here as a non-numeric 'other'. The prior test only spot-checked
    metric_compute/metric_resolve and would NOT have caught a new leaky tool.
    """
    uncategorized: list[str] = []
    for tool in ALLOWED_CHAT_TOOLS:
        underscore = tool.replace(".", "_")
        in_exact = underscore in _EXACT or tool in _EXACT
        if in_exact:
            assert categorize(underscore) != "other", (
                f"{tool} is registered in _EXACT but still categorizes as 'other' — "
                "the _EXACT entry is mis-shaped (check the underscore/dotted key)."
            )
            continue
        if tool in _ALLOWED_OTHER_CHAT_TOOLS:
            # Sanity: an allow-'other' tool must actually be 'other' (else it should
            # be in _EXACT, not here — keeps the two lists from drifting).
            assert categorize(underscore) == "other", (
                f"{tool} is in _ALLOWED_OTHER_CHAT_TOOLS but categorizes as "
                f"'{categorize(underscore)}'. Remove it from the allow-'other' set "
                "(its category now comes from _EXACT)."
            )
            continue
        uncategorized.append(tool)

    assert not uncategorized, (
        "Uncategorized chat tool(s) — their results would be handed to the LLM raw "
        f"(trust-boundary bypass): {sorted(uncategorized)}. Register each in "
        "tool_categories._EXACT (so its result is intercepted as a data_table / "
        "financial / etc.), OR — if it deliberately returns no numbers — add it to "
        "_ALLOWED_OTHER_CHAT_TOOLS in this test with a one-line justification."
    )


def test_allow_other_set_has_no_stale_entries():
    """The allow-'other' escape hatch must not reference tools that no longer exist
    as chat tools (prevents the set from silently hiding a future re-add)."""
    stale = _ALLOWED_OTHER_CHAT_TOOLS - ALLOWED_CHAT_TOOLS
    assert not stale, (
        f"_ALLOWED_OTHER_CHAT_TOOLS references tools not in ALLOWED_CHAT_TOOLS: {sorted(stale)}. "
        "Remove them so the allow-list stays an accurate, reviewed escape hatch."
    )
