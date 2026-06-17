"""Advisory accountant + financial-analytics skills (Phase 1).

Read-only methodology playbooks: NO embedded SuiteQL/tenant columns
(no-prompt-pollution) and NO restated tool figures (no-LLM-numbers). Surfaced via
the existing skill registry + a knowledge profile, and to the user via the
data-driven /skills/catalog menu.
"""
from __future__ import annotations

import pytest

from app.services.chat.knowledge_profiles.loader import load_all_profiles
from app.services.chat.skills import (
    get_all_skills_metadata,
    get_skill_instructions,
    match_skill,
    reload_skills,
)

# slug -> (primary slash, representative routing phrase)
EXPECTED_SKILLS = {
    "pl_flux_variance": ("/flux", "give me a flux analysis on the p&l"),
    "ar_ap_aging_triage": ("/aging", "run an ar aging triage"),
    "ratio_analysis": ("/ratios", "do a ratio analysis for last quarter"),
    "month_end_close": ("/close-checklist", "walk me through the month-end close"),
    "gross_margin_bridge": ("/margin-bridge", "build a gross margin bridge vs last month"),
    "cash_flow_runway": ("/cashflow", "cash flow analysis and runway please"),
    "books_review": ("/books-review", "do a books review for gl hygiene"),
}

# Tenant-schema / SQL markers that must NOT appear in advisory prompts.
POLLUTION_MARKERS = (
    "custitem",
    "transactionline",
    "inventoryitemlocations",
    "builtin.df",
    "iscogs",
    "```sql",
)

# Plain finance asks that must NOT hijack any new advisory skill.
NEGATIVE_PHRASES = (
    "show me revenue",
    "what were sales last month",
    "list our customers",
    "how much did we spend on marketing",
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    reload_skills()
    yield
    reload_skills()


def _slug_meta(slug):
    return next((s for s in get_all_skills_metadata() if s["slug"] == slug), None)


@pytest.mark.parametrize("slug,expected", list(EXPECTED_SKILLS.items()))
def test_skill_registered_with_primary_slash(slug, expected):
    primary_slash, _ = expected
    meta = _slug_meta(slug)
    assert meta is not None, f"skill {slug} not registered"
    assert meta["name"] and meta["description"]
    slash = next((t for t in meta["triggers"] if t.startswith("/")), None)
    assert slash == primary_slash, f"{slug} primary slash {slash!r} != {primary_slash!r}"


@pytest.mark.parametrize("slug", list(EXPECTED_SKILLS))
def test_skill_body_is_methodology_not_sql(slug):
    body = get_skill_instructions(slug)
    assert body, f"no instructions for {slug}"
    low = body.lower()
    for marker in POLLUTION_MARKERS:
        assert marker not in low, f"{slug} body contains pollution marker {marker!r}"


@pytest.mark.parametrize("slug", list(EXPECTED_SKILLS))
def test_skill_body_enforces_no_llm_numbers(slug):
    body = get_skill_instructions(slug)
    assert body
    assert "do not restate" in body.lower(), f"{slug} missing no-LLM-numbers discipline"


@pytest.mark.parametrize("slug,expected", list(EXPECTED_SKILLS.items()))
def test_semantic_phrase_routes_to_skill(slug, expected):
    _, phrase = expected
    matched = match_skill(phrase)
    assert matched is not None, f"phrase {phrase!r} matched no skill"
    assert matched["slug"] == slug, f"phrase {phrase!r} -> {matched['slug']} not {slug}"


@pytest.mark.parametrize("slug,expected", list(EXPECTED_SKILLS.items()))
def test_slash_command_routes_to_skill(slug, expected):
    primary_slash, _ = expected
    matched = match_skill(primary_slash + " for last month")
    assert matched is not None and matched["slug"] == slug


@pytest.mark.parametrize("phrase", NEGATIVE_PHRASES)
def test_plain_finance_asks_do_not_hijack_new_skills(phrase):
    matched = match_skill(phrase)
    if matched is not None:
        assert matched["slug"] not in EXPECTED_SKILLS, (
            f"plain ask {phrase!r} hijacked {matched['slug']}"
        )


def test_all_slash_commands_globally_unique():
    seen = {}
    for s in get_all_skills_metadata():
        for t in s["triggers"]:
            if t.startswith("/"):
                assert t not in seen, f"duplicate slash {t} in {s['slug']} and {seen[t]}"
                seen[t] = s["slug"]


def test_new_semantic_triggers_are_specific_enough():
    for slug in EXPECTED_SKILLS:
        meta = _slug_meta(slug)
        for t in meta["triggers"]:
            if t.startswith("/"):
                continue
            assert len(t.split()) >= 2, f"{slug} trigger {t!r} too generic (<2 words)"


def test_catalog_exposes_new_skills_for_menu():
    metas = {s["slug"]: s for s in get_all_skills_metadata()}
    for slug, (primary_slash, _) in EXPECTED_SKILLS.items():
        assert slug in metas
        assert any(t == primary_slash for t in metas[slug]["triggers"])


def test_financial_analysis_profile_loads_and_activates():
    profiles = {p.profile_id: p for p in load_all_profiles()}
    prof = profiles.get("financial_analysis")
    assert prof is not None, "financial_analysis profile not loaded"
    assert "netsuite_financial_report" in prof.trigger_tools
    assert "metric_compute" in prof.trigger_tools
    assert prof.matches_tools({"netsuite_financial_report"})
    assert prof.matches_tools({"metric_compute"})
    assert not prof.matches_tools({"bigquery_sql"})


def test_financial_analysis_profile_is_pollution_free():
    profiles = {p.profile_id: p for p in load_all_profiles()}
    prof = profiles["financial_analysis"]
    assert prof.prompt_fragment.strip()
    low = prof.prompt_fragment.lower()
    for marker in POLLUTION_MARKERS:
        assert marker not in low, f"profile fragment contains {marker!r}"


def test_existing_skills_still_load():
    slugs = {s["slug"] for s in get_all_skills_metadata()}
    for original in ("period_comparison", "sales_by_platform", "inventory_check", "csv_import_generator"):
        assert original in slugs
