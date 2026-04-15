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
