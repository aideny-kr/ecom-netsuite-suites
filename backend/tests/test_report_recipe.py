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
from app.services.report.report_service import referenced_result_ids, required_result_ids

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


# --- Task 4: financial_statement's compare rids ---------------------------------------


def test_referenced_result_ids_includes_financial_statement_compare_rids():
    sections = [
        {
            "type": "financial_statement",
            "result_id": "r1",
            "statement": "income_statement",
            "period": "Jun 2026",
            "compare": {"prior": "r2", "yoy": "r3", "trend": "r4"},
        }
    ]
    assert referenced_result_ids(sections) == ["r1", "r2", "r3", "r4"]


def test_referenced_result_ids_financial_statement_no_compare():
    sections = [{"type": "financial_statement", "result_id": "r1", "statement": "balance_sheet", "period": "Jun 2026"}]
    assert referenced_result_ids(sections) == ["r1"]


def test_required_result_ids_financial_statement_excludes_compare():
    """Risk 2: only r1 (the current period) is a hard dependency — every compare rid
    (prior/yoy/trend) is optional and degrades that comparison instead."""
    sections = [
        {
            "type": "financial_statement",
            "result_id": "r1",
            "statement": "income_statement",
            "period": "Jun 2026",
            "compare": {"prior": "r2", "yoy": "r3", "trend": "r4"},
        }
    ]
    assert required_result_ids(sections) == {"r1"}


def test_required_result_ids_matches_referenced_for_non_statement_sections():
    """Every OTHER section type keeps pre-Task-4 semantics: everything referenced_result_ids
    finds is required — v1 (table/narrative) recipes and chat compose are unaffected."""
    sections = [
        {"type": "table", "result_id": "r7", "select": ["a"]},
        {"type": "chart", "result_id": "r7"},
        {"type": "narrative", "markdown": "Total {{result:r9.value}} vs {{metric:r12.value}}"},
        {"type": "divider"},
    ]
    assert required_result_ids(sections) == set(referenced_result_ids(sections)) == {"r7", "r9", "r12"}


def test_required_result_ids_mixed_recipe_only_statement_compare_degrades():
    """A recipe mixing a financial_statement section with an ordinary table section:
    the table's result_id is still required, only the statement's compare rids degrade."""
    sections = [
        {
            "type": "financial_statement",
            "result_id": "r1",
            "statement": "income_statement",
            "period": "Jun 2026",
            "compare": {"prior": "r2"},
        },
        {"type": "table", "result_id": "r3"},
    ]
    assert required_result_ids(sections) == {"r1", "r3"}


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


# --- compose wiring: recipe_json rides the Report row; capture never breaks compose ---


def _compose_env(monkeypatch):
    """AsyncMock db capturing added Report rows + neutered RLS/audit (the
    test_report_service.py compose idiom)."""
    import uuid as _uuid
    from unittest.mock import AsyncMock

    from app.services.report import report_service

    monkeypatch.setattr(report_service.audit_service, "log_event", AsyncMock())
    monkeypatch.setattr(report_service, "set_tenant_context", AsyncMock())
    db = AsyncMock()
    added: list = []
    db.add = lambda obj: (added.append(obj), setattr(obj, "id", _uuid.uuid4()))
    return db, added


async def test_compose_report_persists_recipe_json_kwarg(monkeypatch):
    import uuid as _uuid

    from app.services.report import report_service

    db, added = _compose_env(monkeypatch)
    recipe = {"schema_version": 1, "captured_at": "t", "sections": [], "sources": {}}
    await report_service.compose_report(
        db,
        tenant_id=_uuid.uuid4(),
        title="R",
        sections=[{"type": "heading", "level": 1, "text": "R"}],
        resolver=lambda rid: {},
        recipe_json=recipe,
    )
    assert added and added[0].recipe_json == recipe


async def test_compose_report_defaults_recipe_json_none(monkeypatch):
    import uuid as _uuid

    from app.services.report import report_service

    db, added = _compose_env(monkeypatch)
    await report_service.compose_report(
        db,
        tenant_id=_uuid.uuid4(),
        title="R",
        sections=[{"type": "heading", "level": 1, "text": "R"}],
        resolver=lambda rid: {},
    )
    assert added and added[0].recipe_json is None


def _table_payload():
    return {"columns": ["account", "amount"], "rows": [["Cash", 1]], "row_count": 1, "currency_columns": ["amount"]}


async def _run_execute(monkeypatch, sidecar_entries, fallback_messages, sections=None):
    """Drive the REAL report_export.execute with the compose env + patched meta paths."""
    import uuid as _uuid
    from unittest.mock import AsyncMock

    from app.mcp.tools import report_export

    db, added = _compose_env(monkeypatch)
    monkeypatch.setattr(
        "app.services.chat.tool_call_results.load_conversation_tool_messages",
        AsyncMock(return_value=fallback_messages),
    )
    params = {"title": "T", "sections": sections or [{"type": "table", "result_id": "r1", "label": "T"}]}
    ctx = {"db": db, "tenant_id": _uuid.uuid4(), "conversation_id": "conv-1", "actor_id": _uuid.uuid4()}
    with _sidecar(sidecar_entries):
        result = await report_export.execute(params, ctx)
    assert result.get("report_id"), "report must compose regardless of recipe outcome"
    return added[0]


async def test_report_export_execute_captures_recipe_same_turn(monkeypatch):
    report = await _run_execute(
        monkeypatch,
        sidecar_entries={
            "r1": {"payload": _table_payload(), "tool": "netsuite_suiteql", "params": {"query": "SELECT 1"}}
        },
        fallback_messages=[],
    )
    assert report.recipe_json is not None
    assert report.recipe_json["sources"]["r1"] == {
        "tool": "netsuite_suiteql",
        "params": {"query": "SELECT 1"},
        "connection_id": None,
    }
    assert report.recipe_json["sections"] == [{"type": "table", "result_id": "r1", "label": "T"}]


async def test_report_export_execute_cross_turn_recipe_from_persisted(monkeypatch):
    fallback = [_msg([{"tool": _EXT_RUNREPORT, "params": {"reportId": 7}, "result_payload": _table_payload()}])]
    report = await _run_execute(monkeypatch, sidecar_entries={}, fallback_messages=fallback)
    assert report.recipe_json is not None
    assert report.recipe_json["sources"]["r1"]["tool"] == _EXT_RUNREPORT
    assert report.recipe_json["sources"]["r1"]["connection_id"] == str(uuid.UUID(_EXT_HEX))


async def test_report_export_execute_ineligible_tool_composes_without_recipe(monkeypatch):
    """Zero behavior change: a non-allowlisted source tool means recipe_json=None —
    the report itself composes exactly as today."""
    report = await _run_execute(
        monkeypatch,
        sidecar_entries={"r1": {"payload": _table_payload(), "tool": "pivot_query_result", "params": {"rid": "r0"}}},
        fallback_messages=[],
    )
    assert report.recipe_json is None
    assert report.spec_json is not None and report.rendered_html
