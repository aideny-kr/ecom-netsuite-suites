import pytest
from app.services.chat.knowledge_profiles.loader import KnowledgeProfile
from app.services.chat.prompt_assembler import (
    assemble_knowledge_context,
    build_disambiguation_instruction,
    get_active_profiles,
    build_source_pin_hint,
)


_BQ_PROFILE = KnowledgeProfile(
    profile_id="bigquery",
    display_name="BigQuery Analytics",
    trigger_tools=["bigquery_sql", "bigquery_schema"],
    prompt_fragment="## BigQuery Context\nUse LIMIT N.",
    rag_partitions=["bi/schema-docs"],
)
_PRICING_PROFILE = KnowledgeProfile(
    profile_id="pricing",
    display_name="Pricing Engine",
    trigger_tools=["pricing_convert"],
    prompt_fragment="## Pricing Context\nUse pricing_convert.",
    rag_partitions=["pricing/margin-rules"],
)


class TestGetActiveProfiles:
    def test_matches_single(self):
        result = get_active_profiles(
            [_BQ_PROFILE, _PRICING_PROFILE],
            {"bigquery_sql", "netsuite_suiteql"},
        )
        assert len(result) == 1
        assert result[0].profile_id == "bigquery"

    def test_matches_multiple(self):
        result = get_active_profiles(
            [_BQ_PROFILE, _PRICING_PROFILE],
            {"bigquery_sql", "pricing_convert", "netsuite_suiteql"},
        )
        assert len(result) == 2

    def test_matches_none(self):
        result = get_active_profiles(
            [_BQ_PROFILE, _PRICING_PROFILE],
            {"netsuite_suiteql", "rag_search"},
        )
        assert result == []


class TestAssembleKnowledgeContext:
    def test_single_profile_injects_fragment(self):
        result = assemble_knowledge_context([_BQ_PROFILE])
        assert "## BigQuery Context" in result
        assert "LIMIT N" in result

    def test_multiple_profiles_inject_all(self):
        result = assemble_knowledge_context([_BQ_PROFILE, _PRICING_PROFILE])
        assert "## BigQuery Context" in result
        assert "## Pricing Context" in result

    def test_empty_profiles(self):
        result = assemble_knowledge_context([])
        assert result == ""


class TestDisambiguationInstruction:
    def test_injected_when_multiple(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        assert "Multiple Data Sources" in result

    def test_not_injected_when_single(self):
        result = build_disambiguation_instruction([_BQ_PROFILE])
        assert result == ""

    def test_not_injected_when_empty(self):
        result = build_disambiguation_instruction([])
        assert result == ""


class TestDisambiguationPrecedence:
    """The disambiguation prompt MUST honor explicit user source naming.

    Incident: 2026-04-16 staging — user typed "Can you look from NetSuite?"
    after the agent silently chose BigQuery on the prior turn. The existing
    rule "use the most authoritative one" gave the agent latitude to override
    the user's explicit naming. The PRECEDENCE clause closes that hole.
    """

    def test_precedence_clause_present(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        assert "PRECEDENCE" in result

    def test_explicit_naming_examples(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        # Common phrasings the user might type
        assert "in NetSuite" in result
        assert "from BigQuery" in result

    def test_use_only_clause_present(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        # The hard rule: ONLY the named source
        assert "use ONLY that source" in result

    def test_both_source_escape_hatch_documented(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        # The override-the-override: explicit "compare" or "and also" still triggers both
        assert "compare" in result.lower() or "both" in result.lower()

    def test_precedence_appears_before_authoritative_rule(self):
        """Position matters — PRECEDENCE must come first so the model reads it
        before the existing 'most authoritative' fallback."""
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        precedence_pos = result.find("PRECEDENCE")
        authoritative_pos = result.find("most authoritative")
        assert precedence_pos > -1
        assert authoritative_pos > -1
        assert precedence_pos < authoritative_pos

    def test_still_returns_empty_for_single_profile(self):
        # Don't break the existing behavior — only inject when 2+ profiles active
        result = build_disambiguation_instruction([_BQ_PROFILE])
        assert result == ""


class TestSourcePinHint:
    def test_bigquery_pin(self):
        result = build_source_pin_hint("bigquery")
        assert "BigQuery" in result

    def test_netsuite_pin(self):
        result = build_source_pin_hint("netsuite")
        assert "NetSuite" in result

    def test_no_pin(self):
        result = build_source_pin_hint(None)
        assert result == ""
