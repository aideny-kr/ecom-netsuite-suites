"""Phase 5 — the report-generation skill (reporting knowledge profile).

The CEILING on top of the deterministic floor: Phases 1-4 guarantee legible charts,
curated statements, and shape-driven chart types in CODE (prompt guidance alone was
proven insufficient live, twice) — this profile teaches the composing model to exploit
that machinery well (labels, trend results, statement awareness). Nothing here is an
enforcement layer, and nothing hardcodes tenant column/account names (no prompt
pollution) — the guidance is structural only.
"""

from app.services.chat.knowledge_profiles.loader import load_all_profiles


def _reporting_fragment() -> str:
    profiles = load_all_profiles()
    reporting = next(p for p in profiles if p.profile_id == "reporting")
    return reporting.prompt_fragment


def test_reporting_profile_loads_and_triggers_on_report_compose():
    profiles = load_all_profiles()
    reporting = next((p for p in profiles if p.profile_id == "reporting"), None)
    assert reporting is not None
    assert reporting.matches_tools({"report_compose"})


def test_reporting_profile_documents_section_labels():
    """The model should title its data sections — `label` on TABLE and CHART sections
    titles the rendered chart ("Cash Balance Trend"), replacing derived defaults. The
    fragment must show label on all three data-section templates (metric_headline
    already had one → require ≥3 occurrences, i.e. table + chart gained theirs)."""
    frag = _reporting_fragment()
    assert frag.count('"label"') >= 3


def test_reporting_profile_documents_trend_composition():
    """For a trend, compose a compact one-row-per-period result — the renderer picks a
    LINE automatically off the period-shaped x column, so say so (the model should not
    feel obligated to hand-pick chart_type for trends)."""
    frag = _reporting_fragment().lower()
    assert "trend" in frag
    assert "per period" in frag or "one row per period" in frag
    assert "automatic" in frag and "line" in frag


def test_reporting_profile_documents_statement_auto_curation():
    """A NetSuite financial-statement result is auto-curated (key figures + summary
    lines + driver chart) — the model should include the statement table + narrative
    and NOT hand-slice or re-aggregate the statement itself."""
    frag = _reporting_fragment().lower()
    assert "statement" in frag
    assert "automatic" in frag or "automatically" in frag


def test_reporting_profile_still_forbids_inline_numbers():
    """Regression: the no-LLM-numbers rule stays verbatim-strong in the fragment."""
    frag = _reporting_fragment()
    assert "NEVER inline numbers" in frag
