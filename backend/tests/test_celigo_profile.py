"""Tests for the `celigo` knowledge profile (spec
docs/superpowers/specs/2026-09-04-celigo-chat-access.md §7, task 4E).

Mirrors `test_suitescript_workspace_profile.py`'s shape."""

import re

from app.services.chat.knowledge_profiles.loader import load_all_profiles

_CELIGO_TOOL_NAMES = {"celigo_integrations", "celigo_flows", "celigo_flow_steps", "celigo_flow_errors"}

# Any word matching `table`, `column`, `jsonb`, `tenant_id`, `sql`, `select`, or
# `schema` (case-insensitive, as a substring anywhere) would leak an
# implementation/schema detail into the prompt -- the fragment must describe
# intent and honesty only.
_SCHEMA_WORDS = ("table", "column", "jsonb", "tenant_id", "sql", "select", "schema")


class TestCeligoProfile:
    def test_profile_loads(self):
        profiles = load_all_profiles()
        ids = {p.profile_id for p in profiles}
        assert "celigo" in ids

    def test_triggers_on_celigo_flows(self):
        profile = self._get()
        assert profile.matches_tools({"celigo_flows"})

    def test_triggers_on_each_of_the_four_tools(self):
        profile = self._get()
        for name in _CELIGO_TOOL_NAMES:
            assert profile.matches_tools({name}), f"{name} did not trigger the celigo profile"

    def test_does_not_trigger_on_external_celigo_mcp_tool(self):
        """The external hosted-API family is a DIFFERENT surface (task 4E's
        profile only covers the local read-only mirror tools)."""
        profile = self._get()
        assert not profile.matches_tools({"ext__abc__list_flows"})

    def test_does_not_trigger_on_netsuite_suiteql(self):
        profile = self._get()
        assert not profile.matches_tools({"netsuite_suiteql"})

    def test_does_not_trigger_on_bigquery_sql(self):
        profile = self._get()
        assert not profile.matches_tools({"bigquery_sql"})

    def test_key_phrases_appear_in_order(self):
        text = self._get().prompt_fragment.lower()
        phrases = ["verbatim", "never restate", "not a verified zero", "never available", "read-only"]
        positions = [text.index(p) for p in phrases]
        assert positions == sorted(positions), f"key phrases out of order: {list(zip(phrases, positions))}"

    def test_no_stray_celigo_tool_like_tokens(self):
        """No token shaped like a celigo_* tool name other than the four real
        ones -- guards against inventing a fifth tool name in prose."""
        text = self._get().prompt_fragment
        found = set(re.findall(r"celigo_[a-z_]+", text))
        assert found <= _CELIGO_TOOL_NAMES, f"unexpected celigo_-shaped token(s): {found - _CELIGO_TOOL_NAMES}"

    def test_no_table_column_or_schema_words(self):
        text = self._get().prompt_fragment.lower()
        leaked = [w for w in _SCHEMA_WORDS if w in text]
        assert not leaked, f"schema/implementation word(s) leaked into the profile: {leaked}"

    def test_rag_partitions_empty(self):
        assert self._get().rag_partitions == []

    def _get(self):
        for p in load_all_profiles():
            if p.profile_id == "celigo":
                return p
        raise AssertionError("celigo profile not found")
