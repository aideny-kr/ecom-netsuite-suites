"""Structural tests for the netsuite.yaml knowledge profile.

The profile's contract: when any NetSuite read tool is in the toolset,
the SuiteQL dialect rules (~6000 chars) inject into the agent prompt,
and RAG retrieval is scoped to the declared netsuite/* partitions.

These tests fail if:
- profile_id is renamed
- trigger_tools are accidentally dropped or narrowed
- prompt_fragment is accidentally emptied or truncated
- rag_partitions list is accidentally reshaped

They do NOT assert rule content — see test_unified_agent_suiteql_rules.py
for per-rule substring assertions.
"""

import pytest

from app.services.chat.knowledge_profiles.loader import KnowledgeProfile, load_all_profiles


@pytest.fixture(scope="module")
def netsuite_profile() -> KnowledgeProfile:
    profiles = load_all_profiles()
    ns = next((p for p in profiles if p.profile_id == "netsuite"), None)
    assert ns is not None, "netsuite profile missing from knowledge_profiles/"
    return ns


class TestNetSuiteProfileStructure:
    def test_profile_id_is_netsuite(self, netsuite_profile):
        assert netsuite_profile.profile_id == "netsuite"

    def test_display_name_is_set(self, netsuite_profile):
        assert netsuite_profile.display_name == "NetSuite SuiteQL"

    def test_prompt_fragment_is_substantial(self, netsuite_profile):
        # The moved block is ~148 lines / ~6000 chars. A catastrophic
        # regression would leave it empty or tiny.
        assert len(netsuite_profile.prompt_fragment) >= 5000, (
            f"prompt_fragment shrank to {len(netsuite_profile.prompt_fragment)} chars; "
            f"expected >= 5000. Did the SuiteQL block accidentally get truncated?"
        )

    def test_prompt_fragment_wrapped_in_suiteql_dialect_tag(self, netsuite_profile):
        # Cross-references in unified_agent.py tool_selection say "Follow ALL
        # <suiteql_dialect_rules>" — the profile must keep the tag so the
        # reference still resolves in the assembled prompt.
        assert "<suiteql_dialect_rules>" in netsuite_profile.prompt_fragment
        assert "</suiteql_dialect_rules>" in netsuite_profile.prompt_fragment


class TestNetSuiteProfileTriggerTools:
    """The profile must trigger on every NetSuite read-side tool. Saved-search
    and financial-report sessions spill into ad-hoc SuiteQL on follow-ups, so
    all read tools count (write tools stay in netsuite_writes.yaml)."""

    EXPECTED_EXACT_TOOLS = {
        "netsuite_suiteql",
        "netsuite_financial_report",
        "netsuite_get_metadata",
        "ns_runReport",
        "ns_runSavedSearch",
        "ns_listSavedSearches",
        "ns_listAllReports",
    }

    EXPECTED_GLOB_TOOLS = {
        "ext__*__ns_runCustomSuiteQL",
        "ext__*__ns_getSuiteQLMetadata",
        "ext__*__ns_getRecord",
        "ext__*__ns_runReport",
        "ext__*__ns_runSavedSearch",
        "ext__*__ns_listSavedSearches",
        "ext__*__ns_listAllReports",
        "ext__*__ns_getSavedSearchSchema",
    }

    def test_exact_tools_declared(self, netsuite_profile):
        trigger_set = set(netsuite_profile.trigger_tools)
        missing = self.EXPECTED_EXACT_TOOLS - trigger_set
        assert not missing, f"Missing exact-name trigger_tools: {missing}"

    def test_glob_tools_declared(self, netsuite_profile):
        trigger_set = set(netsuite_profile.trigger_tools)
        missing = self.EXPECTED_GLOB_TOOLS - trigger_set
        assert not missing, f"Missing glob trigger_tools: {missing}"

    def test_matches_standard_suiteql_tool(self, netsuite_profile):
        """A tenant with just netsuite_suiteql connected must trigger the profile."""
        assert netsuite_profile.matches_tools({"netsuite_suiteql"})

    def test_matches_ext_mcp_suiteql_tool(self, netsuite_profile):
        """External MCP tool with connector UUID must also trigger via fnmatch glob."""
        assert netsuite_profile.matches_tools({"ext__abc123__ns_runCustomSuiteQL"})

    def test_does_not_match_write_tools(self, netsuite_profile):
        """netsuite_writes.yaml owns the write path; this profile shouldn't trigger for it."""
        # ns_createRecord / ns_updateRecord are write tools handled by
        # netsuite_writes.yaml. netsuite.yaml should NOT trigger on them
        # unless another read-side tool is also present.
        assert not netsuite_profile.matches_tools({"ext__abc123__ns_createRecord"})

    def test_does_not_match_bigquery_only_session(self, netsuite_profile):
        """Tenants with only BigQuery tools should not activate this profile."""
        assert not netsuite_profile.matches_tools({"bigquery_sql", "bigquery_schema"})


class TestNetSuiteProfileRagPartitions:
    """RAG partitions on this profile must match the partition_ids the
    ingest task stamps onto golden_dataset files. If these drift, retrieval
    returns chunks from the wrong partitions (or none).
    """

    EXPECTED_PARTITIONS = {
        "netsuite/suiteql-rules",
        "netsuite/joins",
        "netsuite/transactions",
        "netsuite/multi-currency",
        "netsuite/record-types",
    }

    def test_partition_list_declared(self, netsuite_profile):
        actual = set(netsuite_profile.rag_partitions)
        assert actual == self.EXPECTED_PARTITIONS, (
            f"rag_partitions drift — expected {self.EXPECTED_PARTITIONS}, "
            f"got {actual}. If partitions changed, update ingest frontmatter too."
        )
