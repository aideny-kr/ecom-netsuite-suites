"""Result cards compute every figure from stored payloads (Yucca thread fixtures, 2026-10-01)."""

import pytest

from app.mcp.tools.result_card_tool import (
    CompareResults,
    PresentResult,
    build_compare_card,
    build_present_card,
    describe_mbql,
    sql_aggregates,
)

COUNTRY_SQL = (
    "SELECT BUILTIN.DF(sa.country) AS ship_country, COUNT(DISTINCT t.id) AS orders, "
    "SUM(ABS(tl.quantity)) AS units, ROUND(SUM(tl.amount * -1), 2) AS yucca_line_amount_usd "
    "FROM transaction t JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress "
    "GROUP BY BUILTIN.DF(sa.country) ORDER BY orders DESC"
)
TOTAL_SQL = (
    "SELECT COUNT(DISTINCT t.id) AS orders, SUM(ABS(tl.quantity)) AS units, "
    "SUM(tl.amount * -1) AS yucca_line_amount_usd FROM transaction t"
)
NS_ROWS = [
    ["United States", "162", "170", "1147128"],
    ["Canada", "17", "17", "114865.28"],
    ["Germany", "10", "11", "64793.3"],
    ["Switzerland", "7", "9", "48408.45"],
    ["United Kingdom", "5", "6", "28792.5"],
    ["Australia", "4", "4", "40752.72"],
    ["France", "4", "4", "25463.32"],
    ["Sweden", "2", "2", "12409.34"],
    ["Belgium", "2", "2", "12626.44"],
    ["Spain", "2", "2", "12791.74"],
    ["Netherlands", "2", "2", "12626.44"],
    ["New Zealand", "2", "2", "13406.58"],
    ["Norway", "2", "2", "13401.64"],
    ["Austria", "1", "1", "6365.83"],
    ["Greece", "1", "1", "6241.13"],
    ["Ireland", "1", "1", "6291.87"],
    ["Slovakia", "1", "1", "6291.87"],
    ["Singapore", "1", "1", "6912.48"],
    ["Romania", "1", "1", "5698.82"],
    ["Taiwan (Province of China)", "1", "1", "6513.11"],
]
MB_UNITS = {"United States": 166, "Germany": 10, "Switzerland": 7, "United Kingdom": 5}


def ns_country():
    return {
        "tool": "netsuite_suiteql",
        "as_of": "2026-10-01T05:02:38+00:00",
        "payload": {
            "columns": ["ship_country", "orders", "units", "yucca_line_amount_usd"],
            "rows": NS_ROWS,
            "query": COUNTRY_SQL,
        },
    }


def ns_total():
    return {
        "tool": "netsuite_suiteql",
        "as_of": "2026-10-01T05:01:47+00:00",
        "payload": {
            "columns": ["orders", "units", "yucca_line_amount_usd"],
            "rows": [["228", "240", "1591780.86"]],
            "query": TOTAL_SQL,
        },
    }


def mb_country():
    names = {"New Zealand": "New Zealand/Aotearoa", "Taiwan (Province of China)": "Taiwan"}
    rows = [[names.get(r[0], r[0]), int(r[1]), MB_UNITS.get(r[0], int(r[2]))] for r in NS_ROWS]
    agg = lambda op: [op, {}, ["field", {}, ["db", "public", "spree_line_items", "id"]]]  # noqa: E731
    return {
        "tool": "ext__6f95665c23cc4d35a3f9eb4231099568__query",
        "as_of": "2026-10-01T05:07:03+00:00",
        "payload": {
            "columns": ["c → Name", "Distinct values of o → ID", "Sum of Quantity"],
            "rows": rows,
            "metabase_source": {
                "connector_id": "6f95665c-23cc-4d35-a3f9-eb4231099568",
                "query": {
                    "stages": [
                        {
                            "source-table": ["db", "public", "spree_line_items"],
                            "aggregation": [agg("distinct"), agg("sum")],
                            "breakout": [["field", {}, "c"]],
                        }
                    ]
                },
                "column_sources": ["breakout", "aggregation", "aggregation"],
            },
        },
    }


def mb_total():
    card = mb_country()
    card["payload"] = {**card["payload"], "columns": card["payload"]["columns"][1:], "rows": [[228, 232]]}
    card["payload"]["metabase_source"] = {
        **card["payload"]["metabase_source"],
        "column_sources": ["aggregation", "aggregation"],
    }
    return card


def test_sql_aggregates_follow_the_select_list():
    assert sql_aggregates(COUNTRY_SQL) == {
        "ship_country": "other",
        "orders": "distinct",
        "units": "sum",
        "yucca_line_amount_usd": "sum",
    }


def test_present_card_matches_the_approved_mock():
    spec = PresentResult(
        result_id="r9",
        title="Yucca orders by ship country",
        subtitle="open sales orders",
        row_label_plural="countries",
        columns={
            "ship_country": {"label": "Country"},
            "yucca_line_amount_usd": {"label": "Yucca line value", "format": "currency"},
        },
        sort_by="yucca_line_amount_usd",
        share_of="yucca_line_amount_usd",
        share_label="Share of value",
        control_result_id="r5",
        tiles=True,
    )
    card = build_present_card(spec, ns_country(), ns_total())
    assert [c["label"] for c in card["columns"]] == ["Country", "Orders", "Units", "Yucca line value"]
    assert card["columns"][3]["format"] == "currency" and card["columns"][1]["format"] == "integer"
    assert [r[0] for r in card["rows"][:7]] == [
        "United States",
        "Canada",
        "Germany",
        "Switzerland",
        "Australia",
        "United Kingdom",
        "France",
    ]
    assert card["totals"] == [None, 228, 240, 1591780.86]
    assert card["check"] == {"status": "ok", "text": "The rows add up to the overall total."}
    assert card["share"]["values"][0] == 72.1 and card["share"]["label"] == "Share of value"
    assert card["top_n"] == 7 and card["more_label"] == "Show 13 more countries · 1–2 orders each"
    assert card["totals_label"] == "Total · 20 countries"
    assert [(t["label"], t["value"]) for t in card["tiles"]] == [
        ("Orders", 228),
        ("Units", 240),
        ("Yucca line value", 1591780.86),
        ("Countries", 20),
    ]
    assert card["queries"] == [{"label": "SuiteQL query", "text": COUNTRY_SQL}]


def test_distinct_count_gets_no_total_without_a_control():
    spec = PresentResult(result_id="r9", title="By country")
    card = build_present_card(spec, ns_country(), None)
    assert card["totals"] == [None, None, 240, 1591780.86]
    assert card["check"] is None


def test_overlapping_groups_are_flagged_against_the_control():
    total = ns_total()
    total["payload"]["rows"] = [["200", "240", "1591780.86"]]
    card = build_present_card(PresentResult(result_id="r9", title="t", control_result_id="r5"), ns_country(), total)
    assert card["check"]["status"] == "warn" and "overlap" in card["check"]["text"]
    assert card["totals"][1] == 200


def test_mixed_currency_card_is_collapsed_without_totals():
    payload = {
        "columns": ["currency", "orders", "value"],
        "rows": [["USD", 162, 1147108.0], ["EUR", 24, 176575.06]],
        "query": "SELECT currency, COUNT(*) AS orders, SUM(v) AS value FROM x GROUP BY currency",
    }
    spec = PresentResult(
        result_id="r12",
        title="Value by order currency",
        row_label_plural="currencies",
        collapsed=True,
        no_total_reason="amounts are in different currencies",
    )
    card = build_present_card(spec, {"tool": "netsuite_suiteql", "as_of": "x", "payload": payload}, None)
    assert card["totals"] is None and card["collapsed"]
    assert card["collapsed_note"] == "2 currencies, not added together"


def test_compare_card_matches_the_approved_mock():
    spec = CompareResults(
        left_result_id="r9",
        right_result_id="r21",
        left_label="NetSuite",
        right_label="Metabase",
        key={"left": "ship_country", "right": "c → Name"},
        key_label="Country",
        key_label_plural="countries",
        measures=[
            {"left": "orders", "right": "Distinct values of o → ID", "label": "Orders"},
            {"left": "units", "right": "Sum of Quantity", "label": "Units"},
        ],
        title="Yucca orders by ship country · NetSuite vs Metabase",
        left_control_result_id="r5",
        right_control_result_id="r22",
    )
    card, facts = build_compare_card(spec, ns_country(), mb_country(), (ns_total(), mb_total()))
    assert card["headline"] == "Orders match NetSuite in every country. Units differ in 4 countries."
    assert card["detail"] == (
        "Metabase has 8 fewer units, all in United States, Switzerland, Germany and United Kingdom."
    )
    assert [c["label"] for c in card["columns"]] == [
        "Country",
        "NetSuite",
        "Metabase",
        "NetSuite",
        "Metabase",
        "Difference",
    ]
    assert [c.get("group") for c in card["columns"]] == [None, "Orders", "Orders", "Units", "Units", "Units"]
    assert [r[0] for r in card["rows"][:7]] == [
        "United States",
        "Canada",
        "Germany",
        "Switzerland",
        "United Kingdom",
        "Australia",
        "France",
    ]
    assert card["rows"][0] == ["United States", 162, 162, 170, 166, -4]
    assert card["row_flags"][:3] == ["diff", None, "diff"]
    assert card["totals"] == [None, 228, 228, 240, 232, -8]
    assert card["more_label"] == "Show 13 more countries · identical in both sources"
    names = [r[0] for r in card["rows"]]
    assert "New Zealand" in names and "Taiwan" in names and len(names) == 20
    assert card["check"] == {
        "status": "ok",
        "text": "In each source, the country rows add up to that source's overall total.",
    }
    assert [q["label"] for q in card["queries"]] == ["NetSuite query (SuiteQL)", "Metabase query (query builder)"]
    assert facts["matching"] == ["Orders"] and len(facts["differing"]["Units"]) == 4


def test_duplicate_keys_are_rejected():
    country = ns_country()
    country["payload"]["rows"] = [*NS_ROWS, ["United States", "1", "1", "1"]]
    spec = CompareResults(
        left_result_id="r9",
        right_result_id="r21",
        left_label="A",
        right_label="B",
        key={"left": "ship_country", "right": "c → Name"},
        key_label="Country",
        key_label_plural="countries",
        measures=[{"left": "orders", "right": "Distinct values of o → ID", "label": "Orders"}],
        title="t",
    )
    with pytest.raises(ValueError, match="more than once"):
        build_compare_card(spec, country, mb_country(), (None, None))


def test_describe_mbql_outlines_the_query():
    text = describe_mbql(mb_country()["payload"]["metabase_source"]["query"])
    assert text.startswith("From public.spree_line_items") and "Measure distinct(id), sum(id)" in text


def test_intercept_sends_the_card_to_the_ui_and_only_a_note_to_the_model():
    import json

    from app.services.chat.orchestrator import _intercept_tool_result

    spec = PresentResult(result_id="r9", title="t", control_result_id="r5")
    card = build_present_card(spec, ns_country(), ns_total())
    raw = json.dumps({"result_card": card, "llm": {"card_shown": True, "card_id": card["card_id"], "note": "n"}})
    event, data, condensed = _intercept_tool_result("present_result", raw)
    assert event == "result_card" and data["card_id"] == card["card_id"]
    assert "228" not in condensed and json.loads(condensed)["card_shown"] is True
    assert _intercept_tool_result("present_result", json.dumps({"error": "x"}))[0] is None


# --- T2 gate round 1 on #369 (wf_4744bc2f-d95): one regression per confirmed finding ---


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(amount)/SUM(qty)",
        "ROUND(SUM(a)/COUNT(b), 2)",
        "SUM(x) * 1.0 / COUNT(*)",
        "SUM(DISTINCT v)",
        "ABS(SUM(v))",
        "NVL(SUM(v), 1)",
    ],
)
def test_arithmetic_over_aggregates_is_never_additive(expression):
    assert sql_aggregates(f"SELECT c, {expression} AS m FROM t GROUP BY c")["m"] == "other"


def test_plain_aggregates_keep_their_kind_through_safe_wrappers():
    kinds = sql_aggregates(
        "SELECT c, ROUND(SUM(t.amount * -1), 2) AS a, NVL(SUM(q), 0) AS b, COUNT(*) AS n, "
        "COUNT(DISTINCT t.id) AS d FROM t GROUP BY c"
    )
    assert kinds == {"c": "other", "a": "sum", "b": "sum", "n": "count", "d": "distinct"}


@pytest.mark.parametrize(
    "query",
    [
        "WITH t AS (SELECT region, COUNT(*) AS n FROM x GROUP BY region) SELECT region, n FROM t",
        "SELECT region, COUNT(*) AS n FROM a GROUP BY region UNION ALL SELECT region, COUNT(*) AS n FROM b GROUP BY region",
    ],
)
def test_ctes_and_set_operations_fail_closed(query):
    assert sql_aggregates(query) == {}


def test_detail_rows_keep_identifiers_verbatim_and_get_no_total_row():
    loaded = {
        "tool": "netsuite_suiteql",
        "as_of": "x",
        "payload": {
            "columns": ["customer", "tranid", "zip"],
            "rows": [["Acme", "00123", "02139"], ["Beta", "1E5", None]],
            "query": "SELECT customer, tranid, zip FROM transaction",
        },
    }
    card = build_present_card(PresentResult(result_id="r1", title="Orders"), loaded, None)
    assert card["rows"] == [["Acme", "00123", "02139"], ["Beta", "1E5", None]]
    assert {c["format"] for c in card["columns"]} == {"text"}
    assert card["totals"] is None and card["totals_label"] is None


def test_totals_cover_every_stored_row_not_just_the_displayed_ones():
    rows = [[f"k{i}", "1", "2"] for i in range(800)]
    loaded = {
        "tool": "netsuite_suiteql",
        "as_of": "x",
        "payload": {
            "columns": ["k", "n", "v"],
            "rows": rows,
            "query": "SELECT k, COUNT(*) AS n, SUM(v) AS v FROM t GROUP BY k",
        },
    }
    card = build_present_card(PresentResult(result_id="r1", title="t", sort_by="v"), loaded, None)
    assert card["totals"] == [None, 800, 1600] and card["totals_label"] == "Total · 800 rows"
    assert len(card["rows"]) == 500 and card["truncated"] is True


def test_a_partial_stored_result_gets_no_summed_total():
    loaded = ns_country()
    loaded["payload"] = {**loaded["payload"], "truncated": True, "row_count": 3000}
    card = build_present_card(
        PresentResult(result_id="r9", title="t", tiles=True, row_label_plural="countries"), loaded, None
    )
    assert card["totals"] is None and card["check"]["status"] == "warn" and "partial" in card["check"]["text"]
    assert card["tiles"] == [{"label": "Countries", "value": 3000, "format": "integer"}]


def test_sort_by_keeps_zero_above_negative_values():
    loaded = {
        "tool": "netsuite_suiteql",
        "as_of": "x",
        "payload": {
            "columns": ["k", "v"],
            "rows": [["neg", "-5"], ["zero", "0"], ["pos", "3"]],
            "query": "SELECT k, SUM(v) AS v FROM t GROUP BY k",
        },
    }
    card = build_present_card(PresentResult(result_id="r1", title="t", sort_by="v"), loaded, None)
    assert [r[0] for r in card["rows"]] == ["pos", "zero", "neg"]


def _table(rows, columns=("country", "units")):
    return {
        "tool": "netsuite_suiteql",
        "as_of": "x",
        "payload": {
            "columns": list(columns),
            "rows": rows,
            "query": f"SELECT {columns[0]}, SUM(u) AS {columns[1]} FROM t GROUP BY {columns[0]}",
        },
    }


def _compare(**overrides):
    spec = {
        "left_result_id": "r1",
        "right_result_id": "r2",
        "left_label": "NetSuite",
        "right_label": "Metabase",
        "key": {"left": "country", "right": "country"},
        "key_label": "Country",
        "key_label_plural": "countries",
        "measures": [{"left": "units", "right": "units", "label": "Units"}],
        "title": "t",
        **overrides,
    }
    return CompareResults(**spec)


def test_a_key_or_value_missing_on_one_side_is_a_difference_not_a_match():
    card, facts = build_compare_card(
        _compare(), _table([["X", 1], ["Y", 9], ["Z", 5]]), _table([["X", 1], ["Z", None]]), (None, None)
    )
    assert card["headline"] == "Units differ in 2 countries."
    assert "Only in NetSuite: Y." in card["detail"] and "blank in one source for Z" in card["detail"]
    assert facts["matching"] == []


def test_opposite_differences_are_not_netted_away():
    card, _ = build_compare_card(
        _compare(),
        _table([["Germany", 10], ["Switzerland", 10]]),
        _table([["Germany", 15], ["Switzerland", 5]]),
        (None, None),
    )
    assert card["detail"] == "Metabase has 5 more units in Germany and 5 fewer in Switzerland."


def test_a_one_sided_check_names_the_unchecked_source():
    control = {"tool": "netsuite_suiteql", "as_of": "x", "payload": {"columns": ["units"], "rows": [[10]]}}
    card, _ = build_compare_card(
        _compare(), _table([["X", 4], ["Y", 6]]), _table([["X", 4], ["Y", 6]]), (control, None)
    )
    assert card["check"]["status"] == "warn"
    assert card["check"]["text"] == (
        "NetSuite: the country rows add up to the overall total. Metabase: no overall total to check the rows against."
    )


def test_keys_that_differ_only_in_brackets_are_never_merged():
    left = _table([["Congo (Kinshasa)", 3], ["Congo (Brazzaville)", 2], ["New Zealand", 1]])
    right = _table([["Congo (Kinshasa)", 3], ["Congo (Brazzaville)", 2], ["New Zealand/Aotearoa", 1]])
    card, facts = build_compare_card(_compare(), left, right, (None, None))
    assert sorted(r[0] for r in card["rows"]) == ["Congo (Brazzaville)", "Congo (Kinshasa)", "New Zealand"]
    assert facts["only_in_left"] == [] and facts["only_in_right"] == []


def test_duplicate_measure_labels_are_rejected():
    with pytest.raises(ValueError, match="own label"):
        _compare(
            measures=[{"left": "a", "right": "a", "label": "Units"}, {"left": "b", "right": "b", "label": "units"}]
        )
