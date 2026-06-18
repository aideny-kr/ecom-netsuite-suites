"""Advisory accountant + financial-analytics skills (Phase 1).

Read-only methodology playbooks: NO embedded SuiteQL/tenant columns
(no-prompt-pollution) and NO model-computed figures in prose (no-LLM-numbers — the
tool/query computes & renders; the model gives commentary only). The new skills are
SLASH-ONLY (no semantic phrase triggers) so they never hijack natural-language turns
via the shared substring matcher; the model self-routes from the advertised
/skills/catalog for free-text asks. The GAAP profile triggers on the financial-report
tool only.
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

# slug -> primary (and only) slash command
EXPECTED_SKILLS = {
    "pl_flux_variance": "/flux",
    "ar_ap_aging_triage": "/aging",
    "ratio_analysis": "/ratios",
    "month_end_close": "/close-checklist",
    "gross_margin_bridge": "/margin-bridge",
    "cash_flow_runway": "/cashflow",
    "books_review": "/books-review",
}

# Tenant-schema / SQL markers that must NOT appear in advisory prompts.
POLLUTION_MARKERS = (
    "custitem",
    "transactionline",
    "inventoryitemlocations",
    "builtin.df",
    "iscogs",
    "mainline",
    "taxline",
    "ask-my-accountant",  # QuickBooks-specific account name — no source-specific tokens
    "```sql",
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    reload_skills()
    yield
    reload_skills()


def _slug_meta(slug):
    return next((s for s in get_all_skills_metadata() if s["slug"] == slug), None)


@pytest.mark.parametrize("slug,slash", list(EXPECTED_SKILLS.items()))
def test_skill_registered_with_primary_slash(slug, slash):
    meta = _slug_meta(slug)
    assert meta is not None, f"skill {slug} not registered"
    assert meta["name"] and meta["description"]
    assert slash in meta["triggers"], f"{slug} missing slash trigger {slash!r}"


@pytest.mark.parametrize("slug,slash", list(EXPECTED_SKILLS.items()))
def test_new_skills_are_slash_only(slug, slash):
    # Slash-only by design: no semantic phrase triggers, so the shared substring
    # matcher can never hijack a natural-language turn into one of these skills.
    meta = _slug_meta(slug)
    assert meta["triggers"] == [slash], f"{slug} must be slash-only; has {meta['triggers']!r}"


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
    low = body.lower()
    assert "do not restate" in low, f"{slug} missing no-restate discipline"
    assert "commentary only" in low, f"{slug} missing commentary-only discipline"


@pytest.mark.parametrize("slug,slash", list(EXPECTED_SKILLS.items()))
def test_slash_command_routes_to_skill(slug, slash):
    matched = match_skill(slash + " for last month")
    assert matched is not None and matched["slug"] == slug


def test_all_slash_commands_globally_unique():
    seen = {}
    for s in get_all_skills_metadata():
        for t in s["triggers"]:
            if t.startswith("/"):
                assert t not in seen, f"duplicate slash {t} in {s['slug']} and {seen[t]}"
                seen[t] = s["slug"]


def test_catalog_exposes_new_skills_for_menu():
    metas = {s["slug"]: s for s in get_all_skills_metadata()}
    for slug, slash in EXPECTED_SKILLS.items():
        assert slug in metas
        assert any(t == slash for t in metas[slug]["triggers"])


def test_financial_analysis_profile_trigger_config():
    # NOTE: like every knowledge profile, this activates on tool AVAILABILITY (always-on
    # when the trigger tool is in the inventory — same as netsuite.yaml/bigquery.yaml), NOT
    # on the tool actually being called. The fragment is worded conditionally so it is inert
    # on non-financial turns. This test only pins the trigger configuration.
    prof = {p.profile_id: p for p in load_all_profiles()}["financial_analysis"]
    assert prof.trigger_tools == ["netsuite_financial_report"]
    assert prof.matches_tools({"netsuite_financial_report"})
    assert not prof.matches_tools({"bigquery_sql"})


def test_financial_analysis_profile_is_pollution_free():
    prof = {p.profile_id: p for p in load_all_profiles()}["financial_analysis"]
    assert prof.prompt_fragment.strip()
    low = prof.prompt_fragment.lower()
    for marker in POLLUTION_MARKERS:
        assert marker not in low, f"profile fragment contains {marker!r}"


def test_existing_skills_still_load():
    slugs = {s["slug"] for s in get_all_skills_metadata()}
    for original in ("period_comparison", "sales_by_platform", "inventory_check", "csv_import_generator"):
        assert original in slugs
