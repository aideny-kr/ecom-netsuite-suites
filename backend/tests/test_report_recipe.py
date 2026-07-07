"""Slice A (live-dashboard reports) — recipe capture.

``build_recipe`` assembles ``reports.recipe_json`` at compose time: the LLM's compose
sections VERBATIM + per-result_id {tool, params, connection_id} recovered from the
in-turn sidecar (primary) or the persisted tool_calls (cross-turn fallback) — the same
two paths payload resolution uses, so meta availability tracks payload availability.
Trust boundary: read-only allowlisted tools ONLY; one ineligible/unrecoverable rid ⇒
the WHOLE recipe is omitted (fail closed) and the report composes exactly as today.
Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md §4A.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from app.services.report.recipe import build_recipe, is_recipe_eligible
from app.services.report.report_service import referenced_result_ids

_EXT_HEX = uuid.UUID("0f3c9a2e-0000-0000-0000-0000000beef0").hex
_EXT_RUNREPORT = f"ext__{_EXT_HEX}__ns_runReport"


def _msg(tool_calls):
    return {"role": "assistant", "tool_calls": tool_calls}


def _sidecar(entries: dict):
    """Patch the sidecar entry reader with a dict-backed fake: {rid: envelope}."""
    return patch(
        "app.services.chat.result_cache.get_full_payload_entry",
        side_effect=lambda conv, rid: entries.get(rid),
    )


# --- referenced_result_ids: single-sourced on the fill_placeholders regex ------------


def test_referenced_result_ids_covers_sections_and_placeholders():
    sections = [
        {"type": "heading", "text": "H"},
        {"type": "table", "result_id": "r7", "select": ["a"]},
        {"type": "chart", "result_id": "r7"},  # dupe -> once
        {"type": "narrative", "markdown": "Total {{result:r9.value}} vs {{metric:r12.value}}"},
        {"type": "divider"},
    ]
    assert referenced_result_ids(sections) == ["r7", "r9", "r12"]


def test_referenced_result_ids_tolerates_junk_sections():
    assert referenced_result_ids([{"type": "narrative"}, "junk", None, {}]) == []


# --- the read-only allowlist (fail closed) -------------------------------------------


def test_recipe_eligibility_allowlist():
    # replayable read-only data tools — eligible
    for tool in ("netsuite_suiteql", "netsuite_financial_report", "metric_compute", "bigquery_sql", _EXT_RUNREPORT):
        assert is_recipe_eligible(tool), tool
    # mutations, non-data tools, and the ephemeral pivot — never
    for tool in (
        f"ext__{_EXT_HEX}__ns_createRecord",
        f"ext__{_EXT_HEX}__ns_updateRecord",
        "report_compose",
        "pivot_query_result",
        "pivot.query_result",
        "unknown_tool",
    ):
        assert not is_recipe_eligible(tool), tool


# --- build_recipe: sources, connection ids, fail-closed rules -------------------------


def _table_sections(rid="r1"):
    return [{"type": "table", "result_id": rid, "label": "T"}]


def test_recipe_from_sidecar_meta_local_tool():
    entries = {"r1": {"payload": {"rows": []}, "tool": "netsuite_suiteql", "params": {"query": "SELECT 1"}}}
    with _sidecar(entries):
        recipe = build_recipe(sections=_table_sections(), conversation_id="conv-1", fallback_messages=[])
    assert recipe is not None
    assert recipe["schema_version"] == 1
    assert recipe["captured_at"].endswith("+00:00") or recipe["captured_at"].endswith("Z")
    assert recipe["sources"]["r1"] == {
        "tool": "netsuite_suiteql",
        "params": {"query": "SELECT 1"},
        "connection_id": None,  # local tools re-resolve the tenant connection at replay
    }


def test_recipe_connection_id_parsed_from_ext_tool_name():
    entries = {"r1": {"payload": {}, "tool": _EXT_RUNREPORT, "params": {"reportId": 7}}}
    with _sidecar(entries):
        recipe = build_recipe(sections=_table_sections(), conversation_id="conv-1", fallback_messages=[])
    assert recipe is not None
    assert recipe["sources"]["r1"]["connection_id"] == str(uuid.UUID(_EXT_HEX))  # dashed form


def test_recipe_falls_back_to_persisted_tool_calls_cross_turn():
    fallback = [
        _msg([{"tool": "netsuite_suiteql", "params": {"query": "q1"}, "result_payload": {"rows": [[1]]}}]),
        _msg([{"tool": _EXT_RUNREPORT, "params": {"reportId": 7}, "result_payload": {"rows": [[2]]}}]),
    ]
    sections = [
        {"type": "table", "result_id": "r1"},
        {"type": "narrative", "markdown": "see {{result:r2.value}}"},
    ]
    with _sidecar({}):  # sidecar cold — e.g. compose in a later turn
        recipe = build_recipe(sections=sections, conversation_id="conv-1", fallback_messages=fallback)
    assert recipe is not None
    assert recipe["sources"]["r1"]["tool"] == "netsuite_suiteql"
    assert recipe["sources"]["r2"]["tool"] == _EXT_RUNREPORT


def test_recipe_sidecar_wins_over_fallback_per_rid():
    entries = {"r1": {"payload": {}, "tool": "netsuite_suiteql", "params": {"query": "SIDECAR"}}}
    fallback = [_msg([{"tool": "netsuite_suiteql", "params": {"query": "PERSISTED"}, "result_payload": {"rows": []}}])]
    with _sidecar(entries):
        recipe = build_recipe(sections=_table_sections(), conversation_id="conv-1", fallback_messages=fallback)
    assert recipe["sources"]["r1"]["params"] == {"query": "SIDECAR"}


def test_recipe_sections_are_verbatim_deepcopy():
    sections = _table_sections()
    entries = {"r1": {"payload": {}, "tool": "netsuite_suiteql", "params": {"query": "q"}}}
    with _sidecar(entries):
        recipe = build_recipe(sections=sections, conversation_id="conv-1", fallback_messages=[])
    assert recipe["sections"] == sections
    recipe["sections"][0]["type"] = "MUTATED"  # a recipe consumer can never corrupt compose input
    assert sections[0]["type"] == "table"


def test_recipe_missing_meta_for_any_rid_returns_none():
    entries = {"r1": {"payload": {}, "tool": "netsuite_suiteql", "params": {"query": "q"}}}
    sections = [{"type": "table", "result_id": "r1"}, {"type": "table", "result_id": "r2"}]  # r2 unrecoverable
    with _sidecar(entries):
        assert build_recipe(sections=sections, conversation_id="conv-1", fallback_messages=[]) is None


def test_recipe_meta_less_sidecar_entry_falls_through_then_fails_closed():
    # a pre-deploy envelope ({payload, seq} only) must not satisfy meta
    entries = {"r1": {"payload": {"rows": []}, "seq": 1.0}}
    with _sidecar(entries):
        assert build_recipe(sections=_table_sections(), conversation_id="conv-1", fallback_messages=[]) is None


def test_recipe_ineligible_tool_fails_whole_recipe():
    entries = {
        "r1": {"payload": {}, "tool": "netsuite_suiteql", "params": {"query": "q"}},
        "r2": {"payload": {}, "tool": "pivot_query_result", "params": {"result_id": "r1"}},
    }
    sections = [{"type": "table", "result_id": "r1"}, {"type": "table", "result_id": "r2"}]
    with _sidecar(entries):
        assert build_recipe(sections=sections, conversation_id="conv-1", fallback_messages=[]) is None


def test_recipe_mutation_tool_can_never_enter_a_recipe():
    entries = {"r1": {"payload": {}, "tool": f"ext__{_EXT_HEX}__ns_createRecord", "params": {"type": "customer"}}}
    with _sidecar(entries):
        assert build_recipe(sections=_table_sections(), conversation_id="conv-1", fallback_messages=[]) is None


def test_recipe_no_referenced_rids_returns_none():
    # nothing to re-execute -> snapshot-only (a narrative-only report has no recipe)
    with _sidecar({}):
        assert (
            build_recipe(sections=[{"type": "narrative", "markdown": "hi"}], conversation_id="c", fallback_messages=[])
            is None
        )


def test_recipe_never_raises_even_on_hostile_input():
    class _Boom:
        def get(self, *_a, **_k):
            raise RuntimeError("hostile")

    with patch("app.services.chat.result_cache.get_full_payload_entry", side_effect=RuntimeError("redis")):
        assert build_recipe(sections=[_Boom()], conversation_id="c", fallback_messages=None) is None
