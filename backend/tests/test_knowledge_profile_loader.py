import pytest
from pathlib import Path
from app.services.chat.knowledge_profiles.loader import KnowledgeProfile, load_all_profiles


class TestKnowledgeProfileModel:
    def test_exact_match(self):
        p = KnowledgeProfile(
            profile_id="bigquery",
            display_name="BigQuery",
            trigger_tools=["bigquery_sql", "bigquery_schema"],
            prompt_fragment="## BQ Context",
            rag_partitions=["bi/schema-docs"],
        )
        assert p.matches_tools({"bigquery_sql", "netsuite_suiteql"}) is True

    def test_no_match(self):
        p = KnowledgeProfile(
            profile_id="bigquery",
            display_name="BigQuery",
            trigger_tools=["bigquery_sql"],
            prompt_fragment="## BQ",
            rag_partitions=[],
        )
        assert p.matches_tools({"netsuite_suiteql", "rag_search"}) is False

    def test_glob_match(self):
        p = KnowledgeProfile(
            profile_id="netsuite_writes",
            display_name="NS Writes",
            trigger_tools=["ext__*__ns_createRecord", "ext__*__ns_updateRecord"],
            prompt_fragment="## Writes",
            rag_partitions=[],
        )
        assert p.matches_tools({"ext__a1b2c3d4e5f67890a1b2c3d4e5f67890__ns_createRecord"}) is True

    def test_glob_no_match(self):
        p = KnowledgeProfile(
            profile_id="netsuite_writes",
            display_name="NS Writes",
            trigger_tools=["ext__*__ns_createRecord"],
            prompt_fragment="## Writes",
            rag_partitions=[],
        )
        assert p.matches_tools({"ext__a1b2c3d4e5f67890a1b2c3d4e5f67890__ns_runCustomSuiteQL"}) is False

    def test_empty_trigger_tools(self):
        p = KnowledgeProfile(
            profile_id="empty",
            display_name="Empty",
            trigger_tools=[],
            prompt_fragment="",
            rag_partitions=[],
        )
        assert p.matches_tools({"bigquery_sql"}) is False


class TestLoadAllProfiles:
    def test_loads_yaml_files(self, tmp_path):
        (tmp_path / "test.yaml").write_text(
            "profile_id: test\n"
            "display_name: Test\n"
            "trigger_tools:\n  - foo_tool\n"
            "prompt_fragment: '## Test'\n"
            "rag_partitions: []\n"
        )
        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].profile_id == "test"

    def test_skips_malformed_yaml(self, tmp_path):
        (tmp_path / "good.yaml").write_text(
            "profile_id: good\ndisplay_name: Good\n"
            "trigger_tools: [foo]\nprompt_fragment: ok\nrag_partitions: []\n"
        )
        (tmp_path / "bad.yaml").write_text("not: valid: yaml: [[[")
        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1

    def test_empty_directory(self, tmp_path):
        profiles = load_all_profiles(tmp_path)
        assert profiles == []
