"""Phase 2 — structure-preserving reportData flatten (hierarchy metadata).

``ns_runReport`` flattens to ``[account, amount]``, which DISCARDS the statement
hierarchy — summary/section lines vs detail lines, and indent depth. Phase 3 (a
curated statement + key-figure callouts) needs that structure, but by the time
``report.compose`` resolves the payload only account+amount survive. So the flatten
now carries a parallel ``line_meta`` (per-row ``is_summary`` + ``level``) through the
persisted payload AND the in-turn sidecar — ADDITIVELY: the faithful ``[account,
amount]`` rows are unchanged (the "never drop a figure" invariant holds), and Phase 3
does the actual curation off ``line_meta``.
"""

from __future__ import annotations

import json

from app.services.chat.tool_call_results import (
    _extract_report_data_as_table,
    extract_result_payload,
    report_data_to_capped_table,
)


def _rd(entries: dict) -> str:
    return json.dumps({"reportData": entries})


# --- is_summary: detail vs summary/section line --------------------------------------
def test_line_meta_is_summary_from_is_detail_line():
    cols, rows, meta = _extract_report_data_as_table(
        {
            "0": {"label": "Cash", "isDetailLine": True, "detailLineValues": [{"amount": 100}]},
            "1": {"label": "Total Assets", "isDetailLine": False, "summaryLineValues": [{"Amount": 500}]},
        }
    )
    assert cols == ["account", "amount"]
    assert rows == [["Cash", 100], ["Total Assets", 500]]  # rows UNCHANGED (faithful flatten)
    assert [m["is_summary"] for m in meta] == [False, True]  # detail line vs summary/section line


def test_line_meta_is_summary_inferred_when_is_detail_line_absent():
    # No isDetailLine key: a line with ONLY summaryLineValues is a summary; one with only
    # detailLineValues is a detail line.
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": {"label": "Revenue", "detailLineValues": [{"amount": 100}]},
            "1": {"label": "Gross Profit", "summaryLineValues": [{"Amount": 60}]},
        }
    )
    assert [m["is_summary"] for m in meta] == [False, True]


# --- level: indent depth -------------------------------------------------------------
def test_line_meta_level_captured_from_indent():
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": {"label": "Operating", "isDetailLine": False, "indentLevel": 0, "summaryLineValues": [{"Amount": 1}]},
            "1": {"label": "Salaries", "isDetailLine": True, "indentLevel": 2, "detailLineValues": [{"amount": 2}]},
        }
    )
    assert [m["level"] for m in meta] == [0, 2]


def test_line_meta_defaults_level_zero_when_absent():
    _c, _r, meta = _extract_report_data_as_table(
        {"0": {"label": "X", "isDetailLine": True, "detailLineValues": [{"amount": 1}]}}
    )
    assert meta[0]["level"] == 0


# --- alignment + lockstep through the capped-table helper ----------------------------
def test_line_meta_aligned_to_rows_through_capped_table():
    entries = {str(i): {"label": f"L{i}", "isDetailLine": True, "detailLineValues": [{"amount": i}]} for i in range(5)}
    columns, rows, line_meta, row_count, truncated = report_data_to_capped_table(entries)
    assert columns == ["account", "amount"]
    assert len(line_meta) == len(rows)  # parallel to rows
    assert all(set(m) == {"is_summary", "level", "is_section", "named", "is_leaf"} for m in line_meta)


# --- carried on the PERSISTED payload (== the in-turn sidecar full_payload) -----------
def test_persisted_payload_carries_line_meta_aligned_to_rows():
    payload = extract_result_payload(
        "ext__abc__ns_runReport",
        {},
        _rd(
            {
                "0": {"label": "Cash", "isDetailLine": True, "detailLineValues": [{"amount": 100}]},
                "1": {"label": "Net Income", "isDetailLine": False, "summaryLineValues": [{"Amount": 50}]},
            }
        ),
    )
    assert payload is not None
    assert "line_meta" in payload
    assert len(payload["line_meta"]) == len(payload["rows"])
    assert [m["is_summary"] for m in payload["line_meta"]] == [False, True]


# --- robustness (T2 gate r1): NetSuite "T"/"F" booleans + non-empty-list inference ----
def test_line_meta_isdetailline_boolean_string_t_f():
    # NetSuite serializes booleans as "T"/"F" strings pervasively; BOTH are truthy, so a
    # bare `not bool(isDetailLine)` mislabels every line. Coerce "T"/"F" to a real bool.
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": {"label": "Cash", "isDetailLine": "T", "detailLineValues": [{"amount": 100}]},
            "1": {"label": "Total", "isDetailLine": "F", "summaryLineValues": [{"Amount": 500}]},
        }
    )
    assert [m["is_summary"] for m in meta] == [False, True]  # "T"→detail, "F"→summary


def test_line_meta_inference_uses_nonempty_value_list_not_key_presence():
    # isDetailLine absent + BOTH keys present but summaryLineValues EMPTY. The amount came
    # from detailLineValues (non-empty) → this is a DETAIL line; inference must key off the
    # same non-empty list amount extraction used, not mere key presence.
    _c, _r, meta = _extract_report_data_as_table(
        {"0": {"label": "Cash", "summaryLineValues": [], "detailLineValues": [{"amount": 100}]}}
    )
    assert meta[0]["is_summary"] is False


def test_line_meta_level_handles_float_string_consistently():
    # int("2.0") raises but int(2.0)==2 — coerce via float() so a stringified level parses.
    _c, _r, meta = _extract_report_data_as_table(
        {"0": {"label": "X", "isDetailLine": True, "indentLevel": "2.0", "detailLineValues": [{"amount": 1}]}}
    )
    assert meta[0]["level"] == 2


def test_line_meta_level_falls_through_unparseable_key_to_next(  # T2-gate r2
):
    # {"indentLevel": null, "level": 2}: the loop must not stop at the PRESENT-but-null
    # first key — fall through to the next parseable one instead of defaulting to 0.
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": {
                "label": "X",
                "isDetailLine": True,
                "indentLevel": None,
                "level": 2,
                "detailLineValues": [{"amount": 1}],
            }
        }
    )
    assert meta[0]["level"] == 2


def test_netsuite_bool_coercion_is_distinctly_named():
    # pricing_tools has a SAME-NAMED _coerce_bool with OPPOSITE semantics for "T" (its
    # truthy set is {"true","1","yes"}). The reportData coercer must carry a distinct,
    # NetSuite-explicit name so a grep/copy-paste can't silently invert the hierarchy.
    from app.services.chat import tool_call_results as m

    assert hasattr(m, "_coerce_netsuite_bool")
    assert not hasattr(m, "_coerce_bool")
    assert m._coerce_netsuite_bool("T") is True
    assert m._coerce_netsuite_bool("F") is False


# --- Real-NetSuite statement signals (no indentLevel; hierarchy via label/value/parent) ---
# Real ns_runReport statements (CF/P&L/BS) carry NO indentLevel keys, mark section/total rows
# with a non-null ``label`` (the account name lives in ``value``), leave detail lines UNNAMED
# (value=null,label=null,isDetailLine=true) right after their named account sibling, and nest
# via ``parent`` (null=top). line_meta carries the derived signals so Phase-3 curation works.
def _ns_section(value, amount, parent=None):
    return {"label": "Financial Row", "value": value, "parent": parent, "isDetailLine": False,
            "summaryLineValues": [{"Amount": amount}]}  # fmt: skip


def _ns_account(value, amount, parent="finandim_srawfullname"):
    return {"label": None, "value": value, "parent": parent, "isDetailLine": False,
            "summaryLineValues": [{"Amount": amount}]}  # fmt: skip


def _ns_detail(amount):
    return {"label": None, "value": None, "parent": "finandim_srawvalidname", "isDetailLine": True,
            "detailLineValues": [{"Amount": amount}]}  # fmt: skip


def test_line_meta_is_section_from_label_presence():
    # label!=null marks a section/total row (keyed off PRESENCE, not the string — locale-safe).
    _c, _r, meta = _extract_report_data_as_table(
        {"0": _ns_section("Operating Activities", -100), "1": _ns_account("12000 - Inventory", -50)}
    )
    assert [m["is_section"] for m in meta] == [True, False]


def test_line_meta_named_from_value_presence_marks_junk_grand_total():
    # The unnamed grand-total (value=null, label='Financial Row') is a SECTION but NOT named;
    # curation drops named=False rows so the junk 'Financial Row' line never surfaces.
    _c, rows, meta = _extract_report_data_as_table(
        {
            "0": {
                "label": "Financial Row",
                "value": None,
                "parent": None,
                "isDetailLine": False,
                "summaryLineValues": [{"Amount": 30000}],
            },  # fmt: skip
            "1": _ns_section("Operating Activities", -100),
        }
    )
    assert rows[0] == ["Financial Row", 30000]  # faithful table keeps the figure
    assert meta[0]["is_section"] is True and meta[0]["named"] is False  # but flagged unnamed
    assert meta[1]["named"] is True


def test_line_meta_is_leaf_from_detail_pairing():
    # A named isDetailLine=false row FOLLOWED BY its isDetailLine=true marker is a leaf account;
    # a section (followed by another named row) and an account GROUP are NOT leaves.
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": _ns_section("Operating Activities", -100),  # next is account -> not a leaf
            "1": _ns_account("12000 - Inventory", -50),  # next is a detail marker -> LEAF
            "2": _ns_detail(-50),  # the unnamed detail marker itself -> not a leaf
            "3": _ns_section("Total Operating", -100),  # next is nothing -> not a leaf
        }
    )
    assert [m["is_leaf"] for m in meta] == [False, True, False, False]


def test_line_meta_level_from_parent_when_no_indent():
    # Real statements carry NO indentLevel; derive level from parent (null=0, else=1).
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": _ns_section("Operating Activities", -100, parent=None),
            "1": _ns_section("Net Income", 50, parent="finandim_srawfullname"),
            "2": _ns_account("12000 - Inventory", -50, parent="finandim_srawvalidname"),
        }
    )
    assert [m["level"] for m in meta] == [0, 1, 1]


def test_line_meta_indent_level_still_wins_over_parent():
    # When indentLevel IS present it takes precedence over the parent-derived fallback.
    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": {
                "label": "X",
                "value": "X",
                "parent": None,
                "isDetailLine": False,
                "indentLevel": 3,
                "summaryLineValues": [{"Amount": 1}],
            },  # fmt: skip
        }
    )
    assert meta[0]["level"] == 3
