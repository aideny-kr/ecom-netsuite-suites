"""Tests for the source-switch detector in chat.py send_message."""

from __future__ import annotations

import pytest


class TestSourceSwitchDispatch:
    """These tests assert the pure dispatch logic — no DB, no background task."""

    def test_regex_positive_bigquery(self):
        from app.services.chat.disclosure import _SOURCE_ALIASES, _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match("use BigQuery")
        assert m is not None
        assert _SOURCE_ALIASES.get(m.group(1).lower(), m.group(1).lower()) == "bigquery"

    def test_regex_negative(self):
        from app.services.chat.disclosure import _SOURCE_SWITCH_RE

        assert _SOURCE_SWITCH_RE.match("can I use BigQuery for this?") is None

    def test_regex_positive_switch_to(self):
        from app.services.chat.disclosure import _SOURCE_ALIASES, _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match("switch to netsuite")
        assert m is not None
        alias = _SOURCE_ALIASES.get(m.group(1).lower(), m.group(1).lower())
        assert alias == "netsuite"

    def test_regex_positive_run_on(self):
        from app.services.chat.disclosure import _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match("run on BigQuery")
        assert m is not None

    def test_regex_positive_try(self):
        from app.services.chat.disclosure import _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match("try BigQuery")
        assert m is not None

    def test_regex_negative_embedded_use(self):
        """Embedded 'use BigQuery' in a longer sentence must not match (anchored regex)."""
        from app.services.chat.disclosure import _SOURCE_SWITCH_RE

        assert _SOURCE_SWITCH_RE.match("I want to use BigQuery because it is faster") is None

    def test_alias_bq_maps_to_bigquery(self):
        from app.services.chat.disclosure import _SOURCE_ALIASES, _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match("use bq")
        assert m is not None
        assert _SOURCE_ALIASES.get(m.group(1).lower(), m.group(1).lower()) == "bigquery"

    def test_alias_ns_maps_to_netsuite(self):
        from app.services.chat.disclosure import _SOURCE_ALIASES, _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match("use ns")
        assert m is not None
        assert _SOURCE_ALIASES.get(m.group(1).lower(), m.group(1).lower()) == "netsuite"


class TestGuards:
    """Guard A: prev_msg.disclosure_json.can_switch_source = True
    Guard B: prev_msg.tool_calls contains a data tool
    """

    def test_guard_a_passes_when_can_switch(self):
        prev_disclosure = {"can_switch_source": True, "source": "netsuite"}
        assert prev_disclosure.get("can_switch_source") is True

    def test_guard_a_fails_when_no_disclosure(self):
        prev_disclosure = None
        assert not (prev_disclosure or {}).get("can_switch_source")

    def test_guard_a_fails_when_can_switch_false(self):
        prev_disclosure = {"can_switch_source": False, "source": "netsuite"}
        assert (prev_disclosure or {}).get("can_switch_source") is False

    def test_guard_b_passes_with_data_tool(self):
        tool_calls = [{"tool": "netsuite_suiteql"}]
        assert any(tc.get("tool") in {"netsuite_suiteql", "bigquery_sql"} for tc in tool_calls)

    def test_guard_b_passes_with_bigquery_sql(self):
        tool_calls = [{"tool": "bigquery_sql", "input": {"sql": "SELECT 1"}}]
        assert any(tc.get("tool") in {"netsuite_suiteql", "bigquery_sql"} for tc in tool_calls)

    def test_guard_b_fails_text_only(self):
        tool_calls = None
        assert not any(tc.get("tool") in {"netsuite_suiteql", "bigquery_sql"} for tc in (tool_calls or []))

    def test_guard_b_fails_non_data_tool(self):
        tool_calls = [{"tool": "get_workspace_info"}]
        assert not any(tc.get("tool") in {"netsuite_suiteql", "bigquery_sql"} for tc in tool_calls)


class TestGuardHelpers:
    """Test the actual helper functions from chat.py (when both guards are wired)."""

    def test_prev_turn_can_switch_true(self):
        """_prev_turn_can_switch returns True when disclosure_json.can_switch_source is True."""
        from unittest.mock import MagicMock

        from app.api.v1.chat import _prev_turn_can_switch

        msg = MagicMock()
        msg.disclosure_json = {"can_switch_source": True, "source": "netsuite"}
        assert _prev_turn_can_switch(msg) is True

    def test_prev_turn_can_switch_false_no_disclosure(self):
        from unittest.mock import MagicMock

        from app.api.v1.chat import _prev_turn_can_switch

        msg = MagicMock()
        msg.disclosure_json = None
        assert _prev_turn_can_switch(msg) is False

    def test_prev_turn_can_switch_false_none_msg(self):
        from app.api.v1.chat import _prev_turn_can_switch

        assert _prev_turn_can_switch(None) is False

    def test_prev_turn_has_data_tool_netsuite(self):
        from unittest.mock import MagicMock

        from app.api.v1.chat import _prev_turn_has_data_tool

        msg = MagicMock()
        msg.tool_calls = [{"tool": "netsuite_suiteql", "input": {}}]
        assert _prev_turn_has_data_tool(msg) is True

    def test_prev_turn_has_data_tool_bigquery(self):
        from unittest.mock import MagicMock

        from app.api.v1.chat import _prev_turn_has_data_tool

        msg = MagicMock()
        msg.tool_calls = [{"tool": "bigquery_sql", "input": {}}]
        assert _prev_turn_has_data_tool(msg) is True

    def test_prev_turn_has_data_tool_false_none(self):
        from unittest.mock import MagicMock

        from app.api.v1.chat import _prev_turn_has_data_tool

        msg = MagicMock()
        msg.tool_calls = None
        assert _prev_turn_has_data_tool(msg) is False

    def test_prev_turn_has_data_tool_false_no_match(self):
        from unittest.mock import MagicMock

        from app.api.v1.chat import _prev_turn_has_data_tool

        msg = MagicMock()
        msg.tool_calls = [{"tool": "get_workspace_info"}]
        assert _prev_turn_has_data_tool(msg) is False

    def test_prev_turn_has_data_tool_false_none_msg(self):
        from app.api.v1.chat import _prev_turn_has_data_tool

        assert _prev_turn_has_data_tool(None) is False


class TestPushbackDetection:
    """Pushback regex fires on next-turn telemetry after a disclosed answer."""

    def test_pushback_fires_on_thats_wrong(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match("that's wrong") is not None

    def test_pushback_fires_on_thats_not_right(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match("that's not right") is not None

    def test_pushback_fires_on_actually(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match("actually I need it by region") is not None

    def test_pushback_fires_on_no_i_meant(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match("no, I meant last month") is not None

    def test_pushback_fires_case_insensitive(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match("THAT'S WRONG") is not None

    def test_pushback_does_not_fire_on_normal_query(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        # A normal query should not trigger pushback
        assert _PUSHBACK_RE.match("show me open sales orders") is None

    def test_pushback_does_not_fire_on_data_question(self):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match("what are the top 10 customers") is None
