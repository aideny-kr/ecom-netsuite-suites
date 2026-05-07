"""Tests for the suitescript_workspace knowledge profile."""

from app.services.chat.knowledge_profiles.loader import load_all_profiles


class TestSuitescriptWorkspaceProfile:
    def test_profile_loads(self):
        profiles = load_all_profiles()
        ids = {p.profile_id for p in profiles}
        assert "suitescript_workspace" in ids

    def test_triggers_on_workspace_tool(self):
        profile = self._get()
        assert profile.matches_tools({"workspace_list_files"})
        assert profile.matches_tools({"workspace_run_validate", "bigquery_sql"})

    def test_does_not_trigger_on_bigquery_only(self):
        profile = self._get()
        assert not profile.matches_tools({"bigquery_sql", "bigquery_schema"})

    def test_does_not_trigger_on_suiteql_only(self):
        profile = self._get()
        assert not profile.matches_tools({"netsuite_suiteql"})

    def test_lists_seven_oracle_partitions(self):
        profile = self._get()
        expected = {
            "oracle/ai-connector",
            "oracle/owasp",
            "oracle/sdf-docs",
            "oracle/sdf-roles",
            "oracle/records",
            "oracle/upgrade",
            "oracle/uif-spa",
        }
        assert set(profile.rag_partitions) == expected

    def test_prompt_fragment_mentions_oracle_and_rag(self):
        profile = self._get()
        text = profile.prompt_fragment.lower()
        assert "oracle" in text
        assert "rag" in text or "retrieval" in text

    def _get(self):
        for p in load_all_profiles():
            if p.profile_id == "suitescript_workspace":
                return p
        raise AssertionError("suitescript_workspace profile not found")
