from app.services.chat.knowledge_profiles.loader import KnowledgeProfile, load_all_profiles
from app.services.chat.prompt_assembler import build_disambiguation_instruction


_BQ_PROFILE = KnowledgeProfile(
    profile_id="bigquery",
    display_name="BigQuery Analytics",
    trigger_tools=["bigquery_sql"],
    prompt_fragment="## BQ",
    rag_partitions=[],
)
_NS_PROFILE = KnowledgeProfile(
    profile_id="netsuite_writes",
    display_name="NetSuite",
    trigger_tools=["ext__*__ns_createRecord"],
    prompt_fragment="## NS",
    rag_partitions=[],
)


class TestCrossSourceDisambiguation:
    def test_encourages_both_sources(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _NS_PROFILE])
        assert "call both tools" in result.lower() or "use both" in result.lower()

    def test_does_not_default_to_asking_user(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _NS_PROFILE])
        assert "which would you prefer" not in result.lower()

    def test_still_suggests_asking_when_genuinely_ambiguous(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _NS_PROFILE])
        assert "ask" in result.lower()

    def test_single_profile_returns_empty(self):
        result = build_disambiguation_instruction([_BQ_PROFILE])
        assert result == ""

    def test_empty_profiles_returns_empty(self):
        result = build_disambiguation_instruction([])
        assert result == ""

    def test_cross_source_profile_loads(self):
        profiles = load_all_profiles()
        cross = next((p for p in profiles if p.profile_id == "cross_source"), None)
        assert cross is not None, "cross_source profile not found in loaded profiles"
        assert "bigquery_sql" in cross.trigger_tools
        assert "netsuite_suiteql" in cross.trigger_tools
