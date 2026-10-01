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
    source = card["payload"]["metabase_source"]
    stage = {k: v for k, v in source["query"]["stages"][0].items() if k != "breakout"}
    card["payload"]["metabase_source"] = {
        **source,
        "query": {"stages": [stage]},
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
            "yucca_line_amount_usd": {"label": "Yucca line value", "format": "currency", "currency": "USD"},
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


def test_without_a_control_there_is_no_total_at_all():
    # Totals are the source's own overall figures (gate round 2 on #369): rows are never
    # added up here, so without an ungrouped control the card shows no total row.
    card = build_present_card(PresentResult(result_id="r9", title="By country", tiles=True), ns_country(), None)
    assert card["totals"] is None and card["check"] is None and card["tiles"] == []


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


def test_the_check_covers_every_stored_row_not_just_the_displayed_ones():
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
    control = _control(["n", "v"], ["800", "1600"], "SELECT COUNT(*) AS n, SUM(v) AS v FROM t")
    card = build_present_card(
        PresentResult(result_id="r1", title="t", sort_by="v", control_result_id="r2"), loaded, control
    )
    assert card["totals"] == [None, 800, 1600] and card["totals_label"] == "Total · 800 rows"
    assert card["check"]["status"] == "ok"
    assert len(card["rows"]) == 500 and card["truncated"] is True


def test_a_row_limited_query_never_reads_as_the_whole_population():
    # SELECT ... FETCH FIRST 10 ROWS ONLY comes back "complete": its rows add up to less
    # than the overall figure, and the card says so instead of showing their sum.
    loaded = ns_country()
    loaded["payload"] = {**loaded["payload"], "rows": NS_ROWS[:3]}
    card = build_present_card(
        PresentResult(result_id="r9", title="t", control_result_id="r5", share_of="yucca_line_amount_usd"),
        loaded,
        ns_total(),
    )
    assert card["totals"] == [None, 228, 240, 1591780.86]
    assert card["check"]["status"] == "warn" and "less than the overall total" in card["check"]["text"]
    assert card["share"]["values"][0] == 72.1  # of the real overall total, not of three rows


def test_blank_cells_cannot_be_reconciled():
    loaded = ns_country()
    loaded["payload"] = {**loaded["payload"], "rows": [[*NS_ROWS[0][:3], None], *NS_ROWS[1:]]}
    card = build_present_card(PresentResult(result_id="r9", title="t", control_result_id="r5"), loaded, ns_total())
    assert card["check"]["status"] == "warn" and "cannot be checked" in card["check"]["text"]


def test_a_currency_is_never_guessed():
    loaded = ns_country()
    card = build_present_card(
        PresentResult(result_id="r9", title="t", columns={"yucca_line_amount_usd": {"format": "currency"}}),
        loaded,
        None,
    )
    assert card["columns"][3]["format"] == "number" and card["columns"][3]["currency"] is None


def test_a_partial_stored_result_counts_its_real_rows():
    loaded = ns_country()
    loaded["payload"] = {**loaded["payload"], "truncated": True, "row_count": 3000}
    card = build_present_card(
        PresentResult(result_id="r9", title="t", tiles=True, row_label_plural="countries"), loaded, None
    )
    assert card["totals"] is None and card["truncated"] is True
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


def _control(columns, row, query=None, tool="netsuite_suiteql"):
    """An ungrouped overall query: one row, every column an aggregate, no GROUP BY."""
    query = query or "SELECT " + ", ".join(f"SUM(u) AS {c}" for c in columns) + " FROM t"
    return {"tool": tool, "as_of": "x", "payload": {"columns": list(columns), "rows": [list(row)], "query": query}}


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
    assert "Only in NetSuite: Y." in card["detail"] and "cannot be compared for Z" in card["detail"]
    assert facts["matching"] == []


def test_opposite_differences_are_not_netted_away():
    card, _ = build_compare_card(
        _compare(),
        _table([["Germany", 10], ["Switzerland", 10]]),
        _table([["Germany", 15], ["Switzerland", 5]]),
        (None, None),
    )
    assert card["detail"] == "Metabase has more units in Germany and fewer in Switzerland."


def test_a_one_sided_check_names_the_unchecked_source():
    control = _control(["units"], [10])
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


# --- T2 gate round 2 on #369 (wf_57f49e9b-30f) ---


def test_a_blank_key_is_compared_like_any_other():
    card, facts = build_compare_card(
        _compare(), _table([["X", 1], [None, 4]]), _table([["X", 1], [None, 4]]), (None, None)
    )
    assert card["headline"] == "Units match NetSuite in every country."
    assert sorted(r[0] for r in card["rows"]) == ["(blank)", "X"]
    card, _ = build_compare_card(_compare(), _table([["X", 1], [None, 4]]), _table([["X", 1]]), (None, None))
    assert card["detail"] == "Only in NetSuite: (blank)."


def test_a_partial_comparison_never_claims_every_key_matches():
    left = _table([["X", 1], ["Y", 2]])
    left["payload"]["truncated"] = True
    card, facts = build_compare_card(_compare(), left, _table([["X", 1], ["Y", 2]]), (None, None))
    assert card["headline"] == "Units match NetSuite in every country in both results."
    assert "partial" in card["detail"] and card["truncated"] is True and facts["partial"] is True


def test_two_qualified_names_never_pair_even_alone():
    card, facts = build_compare_card(
        _compare(), _table([["Congo (Kinshasa)", 3]]), _table([["Congo (Brazzaville)", 3]]), (None, None)
    )
    assert facts["only_in_left"] == ["Congo (Kinshasa)"] and facts["only_in_right"] == ["Congo (Brazzaville)"]


def test_unreadable_values_are_never_a_match():
    card, facts = build_compare_card(_compare(), _table([["X", "error"]]), _table([["X", "10x"]]), (None, None))
    assert card["headline"] == "Units differ in 1 country." and facts["matching"] == []


def test_comparison_rows_are_capped_with_differences_first():
    left = _table([[f"k{i}", 1] for i in range(700)])
    right = _table([[f"k{i}", 2 if i == 650 else 1] for i in range(700)])
    card, _ = build_compare_card(_compare(), left, right, (None, None))
    assert len(card["rows"]) == 500 and card["truncated"] is True
    visible = [r[0] for r in card["rows"][: card["top_n"]]]
    assert "k650" in visible and card["row_flags"][visible.index("k650")] == "diff"


async def test_a_pivoted_result_is_refused():
    from app.mcp.tools.result_card_tool import _check_access

    payload = {"columns": ["a"], "rows": [[1]], "source_kind": "metabase", "pivot_provenance": {}}
    with pytest.raises(ValueError, match="pivoted"):
        await _check_access(None, None, "pivot_query_result", payload)


# --- T2 gate round 3 on #369 (wf_a9550ab6-83c) ---


def test_a_text_value_in_a_measure_column_is_shown_as_given_and_flagged():
    loaded = ns_country()
    loaded["payload"] = {**loaded["payload"], "rows": [["United States", "162", "170", "N/A"], *NS_ROWS[1:]]}
    card = build_present_card(PresentResult(result_id="r9", title="t"), loaded, None)
    assert card["rows"][0][3] == "N/A"
    assert card["check"]["status"] == "warn" and "not plain numbers" in card["check"]["text"]


def test_the_model_never_receives_a_cards_rows_on_any_path():
    import json

    from app.services.chat.agents.base_agent import _suppress_metric_value_for_llm

    card = build_present_card(
        PresentResult(result_id="r9", title="t", control_result_id="r5"), ns_country(), ns_total()
    )
    raw = json.dumps({"result_card": card, "llm": {"card_shown": True, "note": "n"}})
    assert json.loads(_suppress_metric_value_for_llm(raw)) == {"card_shown": True, "note": "n"}


def test_key_pairing_scales_linearly():
    import time

    left = _table([[f"left-{i}", 1] for i in range(2000)])
    right = _table([[f"right-{i}", 1] for i in range(2000)])
    started = time.perf_counter()
    build_compare_card(_compare(), left, right, (None, None))
    assert time.perf_counter() - started < 2


def test_a_comparison_without_controls_has_no_total_row():
    card, _ = build_compare_card(_compare(), _table([["X", 1]]), _table([["X", 2]]), (None, None))
    assert card["totals"] is None and card["totals_label"] is None


def test_the_source_label_follows_the_payloads_provenance():
    loaded = {
        "tool": "metric_compute",
        "as_of": "x",
        "payload": {"columns": ["k", "v"], "rows": [["a", 1]], "query": "SELECT 1", "source_kind": "bigquery"},
    }
    card = build_present_card(PresentResult(result_id="r1", title="t"), loaded, None)
    assert card["source"] == "BigQuery" and card["queries"][0]["label"] == "BigQuery SQL"


# --- T2 gate round 4 on #369 (wf_a75d8be0-a99) ---


def test_an_average_gets_its_overall_value_but_no_share_or_reconciliation():
    loaded = {
        "tool": "netsuite_suiteql",
        "as_of": "x",
        "payload": {
            "columns": ["k", "avg_price"],
            "rows": [["a", "10"], ["b", "20"]],
            "query": "SELECT k, SUM(v)/COUNT(*) AS avg_price FROM t GROUP BY k",
        },
    }
    control = _control(["avg_price"], ["15"], "SELECT SUM(v)/COUNT(*) AS avg_price FROM t")
    card = build_present_card(
        PresentResult(result_id="r1", title="t", control_result_id="r2", share_of="avg_price"), loaded, control
    )
    assert card["totals"] == [None, 15] and card["share"] is None and card["check"] is None


def test_rounded_rows_reconcile_within_their_precision():
    loaded = _table([["a", "333.33"], ["b", "333.33"], ["c", "333.34"]])
    control = _control(["units"], ["1000.01"])
    card = build_present_card(PresentResult(result_id="r1", title="t", control_result_id="r2"), loaded, control)
    assert card["check"]["status"] == "ok"


def test_keys_beyond_a_partial_result_are_not_differences():
    left = _table([["X", 1]])
    left["payload"]["truncated"] = True
    card, facts = build_compare_card(_compare(), left, _table([["X", 1], ["Y", 2]]), (None, None))
    assert card["headline"] == "Units match NetSuite in every country in both results."
    assert facts["only_in_right"] == []


def test_no_figure_is_blamed_on_keys_that_hold_none_of_it():
    control = lambda v: _control(["units"], [v])  # noqa: E731
    card, _ = build_compare_card(
        _compare(), _table([["a", 10]]), _table([["a", 11], ["b", 100]]), (control(10), control(111))
    )
    assert card["detail"].startswith("Metabase has more units, all in a.")


def test_comparison_cells_keep_their_source_text():
    card, _ = build_compare_card(_compare(), _table([["X", "N/A"]]), _table([["X", "pending"]]), (None, None))
    assert card["rows"][0][:3] == ["X", "N/A", "pending"]


async def test_an_allowlist_refuses_a_result_with_no_recorded_source():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.mcp.tools.result_card_tool import _check_access

    policy = SimpleNamespace(tool_allowlist=["netsuite_suiteql"], blocked_fields=[])
    with patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=policy)):
        with pytest.raises(ValueError, match="no longer permits"):
            await _check_access(None, None, "", {"columns": ["a"], "rows": [[1]]})


async def test_a_blocked_field_cannot_hide_behind_an_alias():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.mcp.tools.result_card_tool import _check_access

    policy = SimpleNamespace(tool_allowlist=None, blocked_fields=["email"])
    payload = {"columns": ["contact"], "rows": [["a@b.c"]], "query": "SELECT email AS contact FROM customer"}
    with patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=policy)):
        with pytest.raises(ValueError, match="blocked"):
            await _check_access(None, None, "netsuite_suiteql", payload)


def test_a_malformed_stored_result_is_an_error_not_a_crash():
    from app.mcp.tools.result_card_tool import build_present_card as build

    loaded = ns_country()
    loaded["payload"] = {**loaded["payload"], "rows": [["United States", "162"]]}
    with pytest.raises(IndexError):
        build(PresentResult(result_id="r9", title="t"), loaded, None)


# --- independent packet review round 1 on #369 (gpt-6-astra) ---


def test_only_a_provably_overall_query_can_total_a_card():
    # R1: a one-row result is not a total because it has one row. A grouped query, a
    # capped one, a non-aggregate lookup or another source's figure is refused.
    spec = PresentResult(result_id="r9", title="t", control_result_id="r5")
    grouped = _control(
        ["orders", "units"],
        ["162", "170"],
        "SELECT country, COUNT(*) AS orders, SUM(u) AS units FROM t GROUP BY country",
    )
    capped = _control(["units"], ["170"], "SELECT SUM(u) AS units FROM t FETCH FIRST 1 ROWS ONLY")
    lookup = _control(["units"], ["170"], "SELECT units FROM t WHERE id = 5")
    other_source = _control(["units"], ["240"], tool="bigquery_sql")
    metabase_grouped = mb_country()
    metabase_grouped["payload"] = {**metabase_grouped["payload"], "rows": [["United States", 162, 166]]}
    for control in (grouped, capped, lookup, other_source, metabase_grouped):
        with pytest.raises(ValueError, match="overall"):
            build_present_card(spec, ns_country(), control)


def test_rows_that_exceed_a_sum_total_get_no_total_or_share():
    # R1: the rows of a SUM or COUNT partition its total; rows adding up to more than the
    # "total" mean it does not cover them, so neither it nor shares of it are shown.
    control = _control(["units", "yucca_line_amount_usd"], ["100", "1000"])
    card = build_present_card(
        PresentResult(result_id="r9", title="t", control_result_id="r5", share_of="yucca_line_amount_usd", tiles=True),
        ns_country(),
        control,
    )
    assert card["totals"] is None and card["share"] is None and card["tiles"] == []
    assert card["check"]["status"] == "warn" and "does not cover these rows" in card["check"]["text"]


def test_two_blank_values_are_not_a_match():
    # R2: SUM over no values is NULL on both sides; nothing was compared.
    card, facts = build_compare_card(_compare(), _table([["US", None]]), _table([["US", None]]), (None, None))
    assert card["headline"] == "Units differ in 1 country." and facts["matching"] == []
    assert "cannot be compared for US" in card["detail"]


def test_punctuation_never_merges_distinct_keys():
    # R3: SKU A-1 and SKU A/1 are different keys.
    card, facts = build_compare_card(_compare(), _table([["A-1", 10]]), _table([["A/1", 10]]), (None, None))
    assert facts["only_in_left"] == ["A-1"] and facts["only_in_right"] == ["A/1"]
    assert card["headline"] == "Units differ in 2 countries."


def _limited(rows, query):
    table = _table(rows)
    table["payload"]["query"] = query
    return table


def test_a_query_that_reached_its_row_limit_is_partial():
    # R4: NetSuite reports a FETCH FIRST n result as complete; n rows back means more may exist.
    query = "SELECT country, SUM(u) AS units FROM t GROUP BY country ORDER BY units DESC FETCH FIRST 1 ROWS ONLY"
    card, facts = build_compare_card(
        _compare(), _limited([["US", 5]], query), _limited([["US", 5]], query), (None, None)
    )
    assert facts["partial"] is True and "in both results" in card["headline"]
    assert build_present_card(PresentResult(result_id="r1", title="t"), _limited([["US", 5]], query), None)["truncated"]
    # Fewer rows than the limit: the result is complete.
    roomy = _limited([["US", 5]], "SELECT country, SUM(u) AS units FROM t GROUP BY country FETCH FIRST 50000 ROWS ONLY")
    assert build_present_card(PresentResult(result_id="r1", title="t"), roomy, None)["truncated"] is False


def test_a_limit_inside_a_subquery_always_makes_the_result_partial():
    nested = _limited(
        [["US", 5]],
        "SELECT country, SUM(u) AS units FROM (SELECT * FROM t FETCH FIRST 1000 ROWS ONLY) GROUP BY country",
    )
    assert build_present_card(PresentResult(result_id="r1", title="t"), nested, None)["truncated"] is True


def test_a_key_missing_from_a_complete_side_is_a_difference_even_if_the_other_is_partial():
    # R5: CA is definitely absent from the complete right result.
    left = _table([["US", 1], ["CA", 2]])
    left["payload"]["truncated"] = True
    card, facts = build_compare_card(_compare(), left, _table([["US", 3]]), (None, None))
    assert card["headline"] == "Units differ in 2 countries."
    assert sorted(facts["differing"]["Units"]) == ["CA", "US"] and facts["only_in_left"] == ["CA"]
