"""Pure Decimal statement model builder — turns resolved financial_statement report
payloads into the render-ready MODEL Task 3's renderer consumes verbatim.

No DB, no network, no LLM anywhere in this file — every input is a hand-computed fixture
from tests.fixtures.statement_fixture, and every expected total/string is a hand-checked
constant from that same module (never a recomputation of the code under test).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services.report.statement_builder import (
    STATEMENT_ROW_CAP,
    build_statement_model,
    fmt_money,
    fmt_money_delta,
    fmt_pct,
    fmt_pct_delta,
    fmt_pp,
    statement_model_json_safe,
    statement_model_restore_decimals,
)
from tests.fixtures import statement_fixture as fx

# ===========================================================================
# Formatting helpers — unit tests (exact literal strings, not model recomputation)
# ===========================================================================


def test_fmt_money_plain_positive():
    assert fmt_money(Decimal("13093416")) == "$13,093,416"


def test_fmt_money_reduces_profit_shows_parens_of_absolute_value():
    assert fmt_money(Decimal("1700000"), reduces_profit=True) == "($1,700,000)"


def test_fmt_money_reduces_profit_true_on_already_negative_value_still_parens_abs():
    # a negative revenue (contra) line with reduces_profit=True shows the ABSOLUTE value
    # in parens -- never a double-negative "-(-250,000)" mess.
    assert fmt_money(Decimal("-250000"), reduces_profit=True) == "($250,000)"


def test_fmt_money_negative_non_reducing_uses_typographic_minus():
    assert fmt_money(Decimal("-1234")) == "−$1,234"


def test_fmt_money_rounds_half_up_to_whole_dollar():
    assert fmt_money(Decimal("1234.50")) == "$1,235"
    assert fmt_money(Decimal("1234.49")) == "$1,234"


def test_fmt_money_zero():
    assert fmt_money(Decimal("0")) == "$0"
    assert fmt_money(Decimal("0"), reduces_profit=True) == "($0)"


def test_fmt_money_delta_signed():
    assert fmt_money_delta(Decimal("317000")) == "+$317,000"
    assert fmt_money_delta(Decimal("-52000")) == "−$52,000"
    assert fmt_money_delta(Decimal("0")) == "$0"


def test_fmt_pct_basic():
    assert fmt_pct(Decimal("25.9259259")) == "25.9%"
    assert fmt_pct(Decimal("-1.85185")) == "−1.9%"


def test_fmt_pct_negative_zero_normalizes_to_plain_zero():
    # a tiny negative value that rounds to zero at 1dp must never render "-0.0%"
    assert fmt_pct(Decimal("-0.04")) == "0.0%"


def test_fmt_pct_delta_signed():
    assert fmt_pct_delta(Decimal("2.4045")) == "+2.4%"
    assert fmt_pct_delta(Decimal("-2.807")) == "−2.8%"
    assert fmt_pct_delta(Decimal("0")) == "0.0%"


def test_fmt_pp_signed():
    assert fmt_pp(Decimal("0.72680584703644705161810525")) == "+0.7pp"
    assert fmt_pp(Decimal("-0.4")) == "−0.4pp"
    assert fmt_pp(Decimal("0")) == "0.0pp"


# ===========================================================================
# Income statement — full happy path against the 30-account realistic fixture
# ===========================================================================


@pytest.fixture
def is_model():
    return build_statement_model(fx.income_statement_section(), fx.income_statement_payloads())


def test_is_model_top_level_identity(is_model):
    assert is_model["statement"] == "income_statement"
    assert is_model["period"] == "Jun 2026"
    assert is_model["prior_period"] == "May 2026"
    assert is_model["yoy_period"] == "Jun 2025"


def test_is_derivations_exact(is_model):
    # Every derived total is asserted against a hand-checked Decimal constant from the
    # fixture (see fixture module comments for the arithmetic), via the model's formatted
    # KPI value strings -- not recomputed here.
    kpis = {k["key"]: k for k in is_model["kpis"]}
    assert kpis["revenue"]["value"] == "$13,500,000"
    assert kpis["gross_profit"]["value"] == "$3,500,000"
    assert kpis["operating_income"]["value"] == "$1,800,000"
    assert kpis["net_income"]["value"] == "$1,805,000"


def test_is_kpi_margins(is_model):
    kpis = {k["key"]: k for k in is_model["kpis"]}
    assert kpis["revenue"]["margin_pct"] is None  # revenue has no margin of itself
    assert kpis["gross_profit"]["margin_pct"] == fx.EXPECTED_GP_MARGIN_STR
    assert kpis["operating_income"]["margin_pct"] == fx.EXPECTED_OPINC_MARGIN_STR
    assert kpis["net_income"]["margin_pct"] == fx.EXPECTED_NI_MARGIN_STR


def test_is_kpi_mom_deltas(is_model):
    kpis = {k["key"]: k for k in is_model["kpis"]}
    assert kpis["revenue"]["mom_delta"] == fx.EXPECTED_REVENUE_MOM_DELTA_STR
    assert kpis["revenue"]["mom_pct"] == fx.EXPECTED_REVENUE_MOM_PCT_STR
    assert kpis["gross_profit"]["mom_delta"] == fx.EXPECTED_GP_MOM_DELTA_STR
    assert kpis["gross_profit"]["mom_pct"] == fx.EXPECTED_GP_MOM_PCT_STR
    assert kpis["operating_income"]["mom_delta"] == fx.EXPECTED_OPINC_MOM_DELTA_STR
    assert kpis["operating_income"]["mom_pct"] == fx.EXPECTED_OPINC_MOM_PCT_STR
    assert kpis["net_income"]["mom_delta"] == fx.EXPECTED_NI_MOM_DELTA_STR
    assert kpis["net_income"]["mom_pct"] == fx.EXPECTED_NI_MOM_PCT_STR


def test_is_kpi_yoy_pcts(is_model):
    kpis = {k["key"]: k for k in is_model["kpis"]}
    assert kpis["revenue"]["yoy_pct"] == fx.EXPECTED_REVENUE_YOY_PCT_STR
    assert kpis["gross_profit"]["yoy_pct"] == fx.EXPECTED_GP_YOY_PCT_STR
    assert kpis["operating_income"]["yoy_pct"] == fx.EXPECTED_OPINC_YOY_PCT_STR
    assert kpis["net_income"]["yoy_pct"] == fx.EXPECTED_NI_YOY_PCT_STR


def test_is_kpi_sparklines_match_trend_series(is_model):
    kpis = {k["key"]: k for k in is_model["kpis"]}
    assert kpis["revenue"]["spark"] == fx.EXPECTED_TREND_REVENUE
    assert kpis["gross_profit"]["spark"] == fx.EXPECTED_TREND_GROSS_PROFIT
    assert kpis["operating_income"]["spark"] == fx.EXPECTED_TREND_OPERATING_INCOME
    assert kpis["net_income"]["spark"] == fx.EXPECTED_TREND_NET_INCOME
    # spark values are raw Decimal (geometry, not display text) per the model contract
    assert all(isinstance(v, Decimal) for v in kpis["revenue"]["spark"])


def test_is_trend_block(is_model):
    trend = is_model["trend"]
    assert trend is not None
    assert trend["periods"] == fx.EXPECTED_TREND_PERIODS
    series_by_key = {s["key"]: s for s in trend["series"]}
    assert series_by_key["revenue"]["values"] == fx.EXPECTED_TREND_REVENUE
    assert series_by_key["gross_profit"]["values"] == fx.EXPECTED_TREND_GROSS_PROFIT
    assert series_by_key["operating_income"]["values"] == fx.EXPECTED_TREND_OPERATING_INCOME
    assert series_by_key["net_income"]["values"] == fx.EXPECTED_TREND_NET_INCOME


def test_is_quad_headline_rows(is_model):
    quad_by_label = {q["label"]: q for q in is_model["quad"]}
    assert quad_by_label["Revenue"]["current"] == "$13,500,000"
    assert quad_by_label["Revenue"]["prior"] == "$13,183,000"
    assert quad_by_label["Revenue"]["delta"] == fx.EXPECTED_REVENUE_MOM_DELTA_STR
    assert quad_by_label["Revenue"]["delta_pct"] == fx.EXPECTED_REVENUE_MOM_PCT_STR
    assert quad_by_label["Revenue"]["emph"] == "sub"
    assert quad_by_label["Gross Profit"]["emph"] == "formula"
    assert quad_by_label["Operating Income"]["emph"] == "formula"
    assert quad_by_label["Net Income"]["emph"] == "net"
    assert quad_by_label["Net Income"]["current"] == "$1,805,000"


def test_is_sections_present_all_thirty_accounts(is_model):
    total_accounts = sum(len(s["accounts"]) for s in is_model["sections"])
    assert total_accounts == 30
    keys = [s["key"] for s in is_model["sections"]]
    assert keys == ["1-Revenue", "2-Other Income", "3-COGS", "4-Operating Expense", "5-Other Expense"]


def test_is_section_account_formatting_and_pct_rev(is_model):
    revenue_section = next(s for s in is_model["sections"] if s["key"] == "1-Revenue")
    by_number = {a["number"]: a for a in revenue_section["accounts"]}
    product_sales = by_number["4000"]
    assert product_sales["name"] == "Product Sales"
    assert product_sales["current"] == "$12,600,000"
    assert product_sales["prior"] == "$12,300,000"
    assert product_sales["delta"] == "+$300,000"
    assert product_sales["pct_rev"] == "93.3%"
    assert product_sales["reduces_profit"] is False

    returns = by_number["4900"]
    assert returns["current"] == "($250,000)"  # negative revenue line -> parens on abs value
    assert returns["reduces_profit"] is True
    assert returns["pct_rev"] == "−1.9%"


def test_is_section_cogs_account_always_reduces_profit(is_model):
    cogs_section = next(s for s in is_model["sections"] if s["key"] == "3-COGS")
    for account in cogs_section["accounts"]:
        assert account["reduces_profit"] is True
        assert account["current"].startswith("(")


def test_is_section_subtotals(is_model):
    by_key = {s["key"]: s["subtotal"] for s in is_model["sections"]}
    assert by_key["1-Revenue"]["current"] == "$13,500,000"
    assert by_key["1-Revenue"]["label"] == "Total Revenue"
    assert by_key["1-Revenue"]["emph"] == "sub"
    assert by_key["3-COGS"]["current"] == "($10,000,000)"
    assert by_key["3-COGS"]["reduces_profit"] is True
    assert by_key["4-Operating Expense"]["current"] == "($1,700,000)"
    assert by_key["5-Other Expense"]["current"] == "($15,000)"
    assert by_key["2-Other Income"]["current"] == "$20,000"
    assert by_key["2-Other Income"]["reduces_profit"] is False


def test_is_formulas_and_net(is_model):
    formulas_by_label = {f_["label"]: f_ for f_ in is_model["formulas"]}
    assert formulas_by_label["Gross Profit"]["current"] == "$3,500,000"
    assert formulas_by_label["Gross Profit"]["emph"] == "formula"
    assert formulas_by_label["Gross Profit"]["reduces_profit"] is False
    assert formulas_by_label["Operating Income"]["current"] == "$1,800,000"
    net = is_model["net"]
    assert net["label"] == "Net Income"
    assert net["current"] == "$1,805,000"
    assert net["emph"] == "net"
    assert net["reduces_profit"] is False


def test_is_pct_rev_on_summary_rows(is_model):
    # T2 gate M3: the common-size column must not go blank on exactly the rows that
    # matter (design rule #8). Revenue's own subtotal is pinned to 100.0% (it IS the
    # base); GP/OpInc/NI's pct_rev must equal their KPI cards' own margin_pct string
    # (same underlying figure, same fmt_pct formatting -- never independently derived).
    revenue_section = next(s for s in is_model["sections"] if s["key"] == "1-Revenue")
    assert revenue_section["subtotal"]["pct_rev"] == "100.0%"
    kpis = {k["key"]: k for k in is_model["kpis"]}
    formulas_by_label = {f_["label"]: f_ for f_ in is_model["formulas"]}
    assert formulas_by_label["Gross Profit"]["pct_rev"] == kpis["gross_profit"]["margin_pct"]
    assert formulas_by_label["Operating Income"]["pct_rev"] == kpis["operating_income"]["margin_pct"]
    assert is_model["net"]["pct_rev"] == kpis["net_income"]["margin_pct"]
    # every OTHER section's subtotal also gets pct_rev now (not just revenue)
    cogs_section = next(s for s in is_model["sections"] if s["key"] == "3-COGS")
    assert cogs_section["subtotal"]["pct_rev"] is not None


def test_is_checks_empty():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_payloads())
    assert model["checks"] == []


def test_is_watch_items_priority_order_and_cap(is_model):
    watch = is_model["watch"]
    assert len(watch) == 3  # GP margin(1) + up to 2 movers(2) = 3 in this fixture; cap is 4
    assert watch[0]["tone"] == "good"
    assert watch[0]["text"] == "GP margin +0.7pp MoM (25.9% vs 25.2%)"
    assert watch[1]["text"] == "Product Sales +$300,000 MoM (+2.4%)"
    assert watch[1]["tone"] == "good"
    assert watch[2]["text"] == "Marketing Expense +$180,000 MoM (+90.0%)"
    assert watch[2]["tone"] == "warn"


def test_is_highlights(is_model):
    highlights = is_model["highlights"]
    assert len(highlights) == 3
    assert highlights[0] == "Net income −$51,700 MoM, driven by Product Sales +$300,000"
    assert highlights[1] == "Gross margin +0.7pp MoM, driven by Cost of Goods Sold +$100,000"
    assert highlights[2] == "Operating expense +$230,000 MoM, driven by Marketing Expense +$180,000"


def test_is_narrative_two_sentences(is_model):
    narrative = is_model["narrative"]
    assert len(narrative) == 2
    assert narrative[0] == (
        "Income Statement for Jun 2026: revenue was $13,500,000, +2.4% month-over-month, "
        "delivering net income of $1,805,000 (13.4% margin)."
    )
    assert narrative[1] == ("Gross margin was 25.9%, +0.7pp MoM, with operating expenses of $1,700,000, +$230,000 MoM.")


def test_is_ni_driver_scans_all_sections():
    model = build_statement_model(fx.income_statement_section(), fx.ni_driver_cross_section_payloads())
    highlight = next(h for h in model["highlights"] if h.startswith("Net income"))
    assert highlight == (
        f"Net income {fx.EXPECTED_NI_DRIVER_NI_DELTA_STR} MoM, "
        f"driven by {fx.EXPECTED_NI_DRIVER_NAME} {fx.EXPECTED_NI_DRIVER_DELTA_STR}"
    )


def test_determinism_two_identical_calls_produce_identical_output():
    section = fx.income_statement_section()
    payloads = fx.income_statement_payloads()
    first = build_statement_model(section, payloads)
    second = build_statement_model(section, payloads)
    assert first == second


# ===========================================================================
# Degradation contract — never raises for a missing/failed compare source
# ===========================================================================


def test_is_missing_all_compare_sources_degrades_gracefully():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_missing_compare())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == "$13,500,000"  # current-period figures unaffected
    assert kpis["revenue"]["mom_delta"] is None
    assert kpis["revenue"]["mom_pct"] is None
    assert kpis["revenue"]["yoy_pct"] is None
    assert kpis["revenue"]["spark"] is None
    assert model["prior_period"] is None
    assert model["yoy_period"] is None
    assert model["trend"] is None
    assert model["quad"][0]["prior"] is None
    assert model["quad"][0]["delta"] is None
    assert model["sections"][0]["accounts"][0]["prior"] is None
    assert model["sections"][0]["accounts"][0]["delta"] is None
    # T2 gate M1: the WATCH RULES (margin move, movers, trailing-window best/worst) need
    # a compare source and stay silent -- but the model now surfaces an EXPLICIT
    # in-statement signal per expected-but-unresolved comparison, so this is no longer []
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts
    assert "Year-over-year comparison unavailable this run" in watch_texts
    assert "Trend comparison unavailable this run" in watch_texts
    assert all(w["tone"] == "warn" for w in model["watch"])
    assert model["highlights"] == []


def test_is_failed_compare_source_degrades_like_missing():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_failed_compare())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["mom_delta"] is None
    assert kpis["revenue"]["mom_pct"] is None
    assert model["prior_period"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts


def test_no_missing_compare_chip_when_fully_resolved(is_model):
    watch_texts = [w["text"] for w in is_model["watch"]]
    assert not any("unavailable this run" in t for t in watch_texts)


def test_malformed_r1_raises_value_error():
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), fx.malformed_r1_payload())


def test_zero_row_r1_raises_value_error():
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_zero_rows())


def test_zero_row_r1_raises_for_balance_sheet_too():
    with pytest.raises(ValueError):
        build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_payloads_zero_rows())


def test_zero_row_r1_raises_for_trial_balance_too():
    with pytest.raises(ValueError):
        build_statement_model(fx.trial_balance_section(), fx.trial_balance_payloads_zero_rows())


def test_missing_r1_entirely_raises_value_error():
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), {})


def test_malformed_r1_amount_raises_value_error_not_invalid_operation():
    # decimal.InvalidOperation is NOT a ValueError subclass -- if _to_decimal let it
    # escape, this would fail with an unhandled InvalidOperation, not a clean assertion
    # failure. pytest.raises(ValueError) only passes if the seam actually translates it.
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), fx.malformed_r1_amount_payload())


def test_nonfinite_r1_amount_raises_value_error():
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), fx.nonfinite_r1_amount_payload())


def test_malformed_prior_amount_degrades_not_raises():
    # A junk amount in a COMPARE source must never crash the whole build -- it degrades
    # exactly like an absent or success:False r2, same as the other degradation tests.
    model = build_statement_model(fx.income_statement_section(), fx.malformed_prior_amount_payloads())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == "$1,000,000"  # r1 unaffected
    assert kpis["revenue"]["mom_delta"] is None
    assert kpis["revenue"]["mom_pct"] is None
    assert model["prior_period"] is None
    assert model["sections"][0]["accounts"][0]["prior"] is None


# ===========================================================================
# Parse-boundary defensiveness
# ===========================================================================


def test_string_typed_amounts_parsed_as_decimal():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_string_amount_payloads())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == fmt_money(fx.EXPECTED_STRING_AMOUNT_REVENUE)
    assert kpis["gross_profit"]["value"] == fmt_money(fx.EXPECTED_STRING_AMOUNT_GP)


def test_items_derived_payload_shape_is_parsed():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_items_derived_payload())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == fmt_money(fx.EXPECTED_ITEMS_SHAPE_REVENUE)
    assert kpis["gross_profit"]["value"] == fmt_money(fx.EXPECTED_ITEMS_SHAPE_GP)


# ===========================================================================
# Account alignment (acctnumber-keyed, missing-side treated as 0, still listed)
# ===========================================================================


def test_account_alignment_new_and_discontinued_accounts():
    model = build_statement_model(fx.income_statement_section(), fx.misaligned_account_payloads())
    revenue_section = next(s for s in model["sections"] if s["key"] == "1-Revenue")
    by_number = {a["number"]: a for a in revenue_section["accounts"]}

    assert by_number["4000"]["delta"] == fmt_money_delta(fx.EXPECTED_MISALIGNED_4000_DELTA)

    new_line = by_number["4001"]
    assert new_line["current"] == fmt_money(fx.EXPECTED_MISALIGNED_4001_CURRENT)
    assert new_line["prior"] == fmt_money(fx.EXPECTED_MISALIGNED_4001_PRIOR)
    assert new_line["delta"] == fmt_money_delta(fx.EXPECTED_MISALIGNED_4001_DELTA)

    discontinued = by_number["4002"]
    assert discontinued["current"] == fmt_money(fx.EXPECTED_MISALIGNED_4002_CURRENT)
    assert discontinued["prior"] == fmt_money(fx.EXPECTED_MISALIGNED_4002_PRIOR)
    assert discontinued["delta"] == fmt_money_delta(fx.EXPECTED_MISALIGNED_4002_DELTA)
    # "still list the account" -- a prior-only account is NEVER silently dropped
    assert "4002" in by_number


# ===========================================================================
# Watch/highlight threshold straddling — exact boundary, raw (unrounded) precision
# ===========================================================================


def test_gp_margin_watch_fires_just_over_threshold():
    model = build_statement_model(fx.income_statement_section(), fx.gp_margin_watch_payloads("just_over"))
    assert any(w["text"].startswith("GP margin") for w in model["watch"])


def test_gp_margin_watch_silent_just_under_threshold():
    model = build_statement_model(fx.income_statement_section(), fx.gp_margin_watch_payloads("just_under"))
    assert not any(w["text"].startswith("GP margin") for w in model["watch"])


def test_account_mover_watch_fires_just_over_threshold():
    model = build_statement_model(fx.income_statement_section(), fx.account_mover_watch_payloads("just_over"))
    assert any("Marketing Expense" in w["text"] for w in model["watch"])


def test_account_mover_watch_silent_just_under_threshold():
    model = build_statement_model(fx.income_statement_section(), fx.account_mover_watch_payloads("just_under"))
    assert not any("Marketing Expense" in w["text"] for w in model["watch"])


def test_account_mover_tone_uses_own_section_not_a_name_lookup():
    # Two accounts share the acctname "Consulting" in different sections. A tone resolved
    # by re-looking-up "section for this name" (rather than carrying the mover's OWN
    # section through) would silently give both accounts the SAME tone.
    model = build_statement_model(fx.income_statement_section(), fx.duplicate_account_name_watch_payloads())
    consulting_items = [w for w in model["watch"] if w["text"].startswith("Consulting")]
    assert len(consulting_items) == 2
    # ranked by |delta| descending: the larger REVENUE mover (+300,000, favorable) is
    # first with tone "good"; the smaller OPEX mover (+200,000, unfavorable) is second
    # with tone "warn".
    assert consulting_items[0]["text"] == fx.EXPECTED_DUP_NAME_REVENUE_MOVER_TEXT
    assert consulting_items[0]["tone"] == "good"
    assert consulting_items[1]["text"] == fx.EXPECTED_DUP_NAME_OPEX_MOVER_TEXT
    assert consulting_items[1]["tone"] == "warn"


def test_highlight_fires_just_over_threshold():
    model = build_statement_model(fx.income_statement_section(), fx.highlight_threshold_payloads("just_over"))
    assert any(h.startswith("Gross margin") for h in model["highlights"])


def test_highlight_silent_just_under_threshold():
    model = build_statement_model(fx.income_statement_section(), fx.highlight_threshold_payloads("just_under"))
    assert not any(h.startswith("Gross margin") for h in model["highlights"])


def test_watch_rule3_fires_good_when_current_is_trailing_six_max():
    model = build_statement_model(fx.income_statement_section(), fx.ni_margin_trend_best_payloads())
    rule3 = [w for w in model["watch"] if "trailing 6" in w["text"]]
    assert len(rule3) == 1
    assert rule3[0]["tone"] == "good"
    assert "best month in trailing 6" in rule3[0]["text"]


def test_watch_rule3_fires_bad_when_current_is_trailing_six_min():
    model = build_statement_model(fx.income_statement_section(), fx.ni_margin_trend_worst_payloads())
    rule3 = [w for w in model["watch"] if "trailing 6" in w["text"]]
    assert len(rule3) == 1
    assert rule3[0]["tone"] == "bad"
    assert "worst month in trailing 6" in rule3[0]["text"]


def test_watch_rule3_silent_when_current_is_neither_max_nor_min(is_model):
    # the main 30-account fixture's Jun 2026 sits in the middle of its trailing-6 window
    assert not any("trailing 6" in w["text"] for w in is_model["watch"])


def test_trend_buckets_order_by_parsed_period_not_raw_startdate_string():
    # LIVE SuiteQL returns startdate as "M/D/YYYY", not the ISO fixture convention every
    # other trend test uses -- a raw string sort over that scrambles a cross-year window
    # (see fixture docstring for the exact lexicographic mechanics). Bucket order AND
    # rule 3's current-period attribution must both be correct.
    section = fx.income_statement_section(period="Jan 2027")
    model = build_statement_model(section, fx.ni_margin_trend_cross_year_live_date_format_payloads())
    assert model["trend"]["periods"] == fx.EXPECTED_CROSS_YEAR_TREND_PERIODS
    rule3 = [w for w in model["watch"] if "trailing 6" in w["text"]]
    assert len(rule3) == 1
    assert rule3[0]["tone"] == "good"
    assert rule3[0]["text"] == "NI margin best month in trailing 6 (20.0%)"


# ===========================================================================
# Balance sheet — full happy path
# ===========================================================================


@pytest.fixture
def bs_model():
    return build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_payloads())


def test_bs_top_level_identity(bs_model):
    assert bs_model["statement"] == "balance_sheet"
    assert bs_model["prior_period"] == "May 2026"
    assert bs_model["yoy_period"] is None  # balance_sheet never gets a yoy compare source
    assert bs_model["trend"] is None  # no trend source for balance_sheet
    assert bs_model["formulas"] is None  # IS-only
    assert bs_model["net"] is None  # IS-only


def test_bs_kpis(bs_model):
    kpis = {k["key"]: k for k in bs_model["kpis"]}
    assert kpis["total_assets"]["value"] == "$6,550,000"
    assert kpis["total_assets"]["mom_delta"] == fx.EXPECTED_BS_ASSETS_MOM_DELTA_STR
    assert kpis["total_assets"]["margin_pct"] is None
    assert kpis["total_liabilities"]["value"] == "$2,750,000"
    assert kpis["total_liabilities"]["mom_delta"] == fx.EXPECTED_BS_LIABILITIES_MOM_DELTA_STR
    assert kpis["total_equity"]["value"] == "$3,800,000"
    assert kpis["total_equity"]["mom_delta"] == fx.EXPECTED_BS_EQUITY_MOM_DELTA_STR


def test_bs_kpis_marked_neutral(bs_model):
    # T2 gate minor[9]: a BS KPI's "increase" has no inherent favorability (design rule
    # #10 — color is EXCLUSIVELY favorable/unfavorable, never decoration) — the renderer
    # needs an explicit signal to render these deltas colorless.
    for kpi in bs_model["kpis"]:
        assert kpi["neutral"] is True


def test_tb_kpis_marked_neutral(tb_model):
    for kpi in tb_model["kpis"]:
        assert kpi["neutral"] is True


def test_is_kpis_not_neutral(is_model):
    for kpi in is_model["kpis"]:
        assert kpi["neutral"] is False


def test_bs_sections_three_groups(bs_model):
    keys = [s["key"] for s in bs_model["sections"]]
    assert keys == ["1-Assets", "2-Liabilities", "3-Equity"]
    total_accounts = sum(len(s["accounts"]) for s in bs_model["sections"])
    assert total_accounts == 11  # 5 assets + 4 liabilities + 2 equity


def test_bs_account_reduces_profit_always_false(bs_model):
    for section in bs_model["sections"]:
        for account in section["accounts"]:
            assert account["reduces_profit"] is False
            assert account["pct_rev"] is None  # no revenue concept on a balance sheet


def test_bs_summary_rows_have_no_pct_rev(bs_model):
    # M3: only IS gets a common-size column -- BS has no revenue base to compute
    # against, so its subtotal rows stay None (never a fabricated "% of assets" figure
    # the brief never asked for).
    for section in bs_model["sections"]:
        assert section["subtotal"]["pct_rev"] is None


def test_bs_no_missing_compare_chip_when_fully_resolved(bs_model):
    watch_texts = [w["text"] for w in bs_model["watch"]]
    assert not any("unavailable this run" in t for t in watch_texts)


def test_bs_check_in_balance(bs_model):
    checks = bs_model["checks"]
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["label"] == "Assets = Liabilities + Equity"


def test_bs_check_out_of_balance():
    model = build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_unbalanced_payloads())
    checks = model["checks"]
    assert checks[0]["ok"] is False
    assert fmt_money(fx.EXPECTED_BS_UNBALANCED_DIFF) in checks[0]["detail"]


def test_bs_narrative(bs_model):
    narrative = bs_model["narrative"]
    assert len(narrative) == 2
    assert narrative[0] == "Balance sheet as of Jun 2026: total assets of $6,550,000, +$250,000 month-over-month."
    assert "in balance" in narrative[1]


def test_bs_missing_compare_degrades():
    model = build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_payloads_missing_compare())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["total_assets"]["mom_delta"] is None
    assert model["prior_period"] is None
    assert model["sections"][0]["accounts"][0]["prior"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts


# ===========================================================================
# Trial balance — full happy path (no section column, flat account listing)
# ===========================================================================


@pytest.fixture
def tb_model():
    return build_statement_model(fx.trial_balance_section(), fx.trial_balance_payloads())


def test_tb_top_level_identity(tb_model):
    assert tb_model["statement"] == "trial_balance"
    assert tb_model["yoy_period"] is None
    assert tb_model["trend"] is None
    assert tb_model["formulas"] is None
    assert tb_model["net"] is None


def test_tb_kpis_have_mom_deltas(tb_model):
    # TB's recipe fetches r2 the same as every other statement type -- its headline
    # figures must actually use it (mirrors BS's total_assets/liabilities/equity pattern).
    kpis = {k["key"]: k for k in tb_model["kpis"]}
    assert kpis["total_debits"]["value"] == "$1,600,000"
    assert kpis["total_debits"]["mom_delta"] == fx.EXPECTED_TB_DEBIT_MOM_DELTA_STR
    assert kpis["total_debits"]["mom_pct"] == fx.EXPECTED_TB_DEBIT_MOM_PCT_STR
    assert kpis["total_credits"]["value"] == "$1,600,000"
    assert kpis["total_credits"]["mom_delta"] == fx.EXPECTED_TB_CREDIT_MOM_DELTA_STR
    assert kpis["total_credits"]["mom_pct"] == fx.EXPECTED_TB_CREDIT_MOM_PCT_STR


def test_tb_quad_has_mom_deltas(tb_model):
    quad_by_label = {q["label"]: q for q in tb_model["quad"]}
    assert quad_by_label["Total Debits"]["prior"] == "$1,480,000"
    assert quad_by_label["Total Debits"]["delta"] == fx.EXPECTED_TB_DEBIT_MOM_DELTA_STR
    assert quad_by_label["Total Credits"]["prior"] == "$1,480,000"
    assert quad_by_label["Total Credits"]["delta"] == fx.EXPECTED_TB_CREDIT_MOM_DELTA_STR


def test_tb_sections_flat_no_gaap_grouping(tb_model):
    assert len(tb_model["sections"]) == 1
    section = tb_model["sections"][0]
    assert len(section["accounts"]) == 6
    by_number = {a["number"]: a for a in section["accounts"]}
    assert by_number["1000"]["current"] == "$1,000,000"  # net_amount, plain (reduces_profit False for TB)
    assert by_number["1000"]["reduces_profit"] is False
    assert by_number["1000"]["delta"] == fmt_money_delta(fx.EXPECTED_TB_CASH_DELTA)


def test_tb_check_in_balance(tb_model):
    checks = tb_model["checks"]
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["label"] == "Debits = Credits"


def test_tb_check_out_of_balance():
    model = build_statement_model(fx.trial_balance_section(), fx.trial_balance_unbalanced_payloads())
    checks = model["checks"]
    assert checks[0]["ok"] is False
    assert fmt_money(fx.EXPECTED_TB_UNBALANCED_DIFF) in checks[0]["detail"]


def test_tb_narrative(tb_model):
    narrative = tb_model["narrative"]
    assert len(narrative) == 2
    assert narrative[0] == "Trial balance for Jun 2026: total debits of $1,600,000, total credits of $1,600,000."
    assert narrative[1] == "The trial balance is in balance."


def test_tb_narrative_out_of_balance_states_difference():
    model = build_statement_model(fx.trial_balance_section(), fx.trial_balance_unbalanced_payloads())
    assert "out of balance" in model["narrative"][1]
    assert fmt_money(fx.EXPECTED_TB_UNBALANCED_DIFF) in model["narrative"][1]


def test_tb_missing_compare_degrades():
    model = build_statement_model(fx.trial_balance_section(), fx.trial_balance_payloads_missing_compare())
    assert model["prior_period"] is None
    assert model["sections"][0]["accounts"][0]["prior"] is None
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["total_debits"]["mom_delta"] is None
    assert kpis["total_debits"]["mom_pct"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts
    assert kpis["total_credits"]["mom_delta"] is None
    quad_by_label = {q["label"]: q for q in model["quad"]}
    assert quad_by_label["Total Debits"]["prior"] is None
    assert quad_by_label["Total Debits"]["delta"] is None


# ===========================================================================
# Row-cap guard — the statement SQL templates cap at STATEMENT_ROW_CAP rows; a payload
# that lands exactly on the cap may have silently truncated a larger tenant's real
# account list, corrupting totals/NI/the balance check. Must warn, not stay silent.
# ===========================================================================


def test_row_cap_guard_fires_at_exactly_the_cap():
    model = build_statement_model(fx.income_statement_section(), fx.row_cap_boundary_payloads(STATEMENT_ROW_CAP))
    cap_items = [w for w in model["watch"] if "row cap reached" in w["text"]]
    assert len(cap_items) == 1
    assert cap_items[0]["tone"] == "warn"
    assert cap_items[0]["text"] == f"row cap reached — totals may be incomplete ({STATEMENT_ROW_CAP} accounts)"


def test_row_cap_guard_silent_just_under_the_cap():
    model = build_statement_model(fx.income_statement_section(), fx.row_cap_boundary_payloads(STATEMENT_ROW_CAP - 1))
    assert not any("row cap reached" in w["text"] for w in model["watch"])


def test_row_cap_guard_wired_into_balance_sheet():
    model = build_statement_model(fx.balance_sheet_section(), fx.bs_row_cap_boundary_payloads(STATEMENT_ROW_CAP))
    assert any("row cap reached" in w["text"] for w in model["watch"])
    below = build_statement_model(fx.balance_sheet_section(), fx.bs_row_cap_boundary_payloads(STATEMENT_ROW_CAP - 1))
    assert not any("row cap reached" in w["text"] for w in below["watch"])


def test_row_cap_guard_wired_into_trial_balance():
    model = build_statement_model(fx.trial_balance_section(), fx.tb_row_cap_boundary_payloads(STATEMENT_ROW_CAP))
    assert any("row cap reached" in w["text"] for w in model["watch"])
    below = build_statement_model(fx.trial_balance_section(), fx.tb_row_cap_boundary_payloads(STATEMENT_ROW_CAP - 1))
    assert not any("row cap reached" in w["text"] for w in below["watch"])


# ===========================================================================
# T2 gate round 2, B1(b) — a TRUNCATED payload (an extraction-layer cap fired below the
# SQL's own STATEMENT_ROW_CAP) must be detected, distinctly from the row-cap guard above
# (which only fires when the statement's OWN row count lands exactly on the SQL cap). r1
# fails closed; a truncated compare degrades like an absent/failed one.
# ===========================================================================


def test_truncated_r1_raises_value_error():
    with pytest.raises(ValueError, match="account list truncated at 100 of 6000"):
        build_statement_model(fx.income_statement_section(), fx.truncated_r1_payload())


def test_truncated_r1_error_message_states_statement_cannot_be_computed():
    with pytest.raises(ValueError, match="statement cannot be computed completely"):
        build_statement_model(fx.income_statement_section(), fx.truncated_r1_payload())


def test_truncated_r1_raises_for_balance_sheet_too():
    rows = [_bs_row_for_test(i) for i in range(50)]
    payload = fx._payload(fx._BS_COLUMNS, rows, query="balance_sheet (2026-06-30)")
    payload["truncated"] = True
    payload["row_count"] = 9000
    with pytest.raises(ValueError, match="account list truncated at 50 of 9000"):
        build_statement_model(fx.balance_sheet_section(), {"r1": payload})


def _bs_row_for_test(i: int) -> dict:
    return {
        "acctnumber": str(1000 + i),
        "acctname": f"Asset {i}",
        "accttype": "Bank",
        "section": "1-Assets",
        "balance": Decimal("1"),
    }


def test_truncated_compare_degrades_not_raises():
    model = build_statement_model(fx.income_statement_section(), fx.truncated_compare_payload())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == "$5"  # r1 (5 accounts, $1 each) unaffected
    assert kpis["revenue"]["mom_delta"] is None
    assert kpis["revenue"]["mom_pct"] is None
    assert model["prior_period"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts


def test_row_count_greater_than_len_rows_without_truncated_flag_still_detected():
    """A payload could theoretically set row_count > len(rows) without also flipping the
    truncated flag (defensive: the two signals should never disagree, but detection must
    not depend on both being set correctly)."""
    payloads = fx.row_cap_boundary_payloads(100)
    payloads["r1"]["truncated"] = False
    payloads["r1"]["row_count"] = 500
    with pytest.raises(ValueError, match="account list truncated at 100 of 500"):
        build_statement_model(fx.income_statement_section(), payloads)


# ===========================================================================
# T2 gate round 2, M-B — a compare source that resolves successfully but is legitimately
# EMPTY (a derived period with zero rows) must degrade like an absent/failed source,
# never render $0 priors / deltas computed against nothing.
# ===========================================================================


def test_empty_compare_degrades_not_zero():
    model = build_statement_model(fx.income_statement_section(), fx.empty_compare_payload())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == "$5"  # r1 (5 accounts, $1 each) unaffected
    assert kpis["revenue"]["mom_delta"] is None, "an empty compare must NOT compute a delta vs $0"
    assert kpis["revenue"]["mom_pct"] is None
    assert model["prior_period"] is None
    assert model["quad"][0]["prior"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts


def test_r1_zero_rows_still_raises_unaffected_by_mb_fix():
    """M-B only changes COMPARE handling; r1's existing (wave-4) zero-row fail-closed
    behavior must be completely unchanged."""
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_zero_rows())


# ===========================================================================
# Unknown statement type — malformed section input
# ===========================================================================


def test_unknown_statement_type_raises_value_error():
    section = fx.income_statement_section()
    section["statement"] = "cash_flow"  # not one of the three supported types
    with pytest.raises(ValueError):
        build_statement_model(section, fx.income_statement_payloads())


# ===========================================================================
# Task 4 (Risk 3) — JSON-persistence boundary: kpis[].spark / trend.series[].values
# ===========================================================================


def test_statement_model_json_safe_converts_decimal_spark_and_trend_to_strings(is_model):
    safe = statement_model_json_safe(is_model)
    kpis = {k["key"]: k for k in safe["kpis"]}
    assert all(isinstance(v, str) for v in kpis["revenue"]["spark"])
    assert all(isinstance(v, str) for v in safe["trend"]["series"][0]["values"])
    # the ORIGINAL model is untouched (no mutation) — still real Decimal
    kpis_orig = {k["key"]: k for k in is_model["kpis"]}
    assert all(isinstance(v, Decimal) for v in kpis_orig["revenue"]["spark"])


def test_statement_model_json_safe_is_actually_json_dumpable(is_model):
    safe = statement_model_json_safe(is_model)
    # this would raise TypeError on a bare Decimal — the exact crash Risk 3 fixes
    assert json.dumps(safe)


def test_statement_model_json_safe_no_trend_or_spark_is_a_noop():
    """balance_sheet/trial_balance never carry a trend and their kpis never carry a
    spark — the sanitizer must not choke on (or invent) either."""
    model = build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_payloads())
    safe = statement_model_json_safe(model)
    assert safe["trend"] is None
    assert all(k["spark"] is None for k in safe["kpis"])
    assert json.dumps(safe)


def test_statement_model_restore_decimals_is_the_inverse(is_model):
    safe = statement_model_json_safe(is_model)
    restored = statement_model_restore_decimals(safe)
    kpis = {k["key"]: k for k in restored["kpis"]}
    assert kpis["revenue"]["spark"] == fx.EXPECTED_TREND_REVENUE  # exact Decimal values back
    assert all(isinstance(v, Decimal) for v in kpis["revenue"]["spark"])
    assert all(isinstance(v, Decimal) for v in restored["trend"]["series"][0]["values"])


def test_statement_model_json_round_trip_renders_identically(is_model):
    """The brief's binding round-trip requirement: build -> json.dumps -> json.loads ->
    render produces byte-IDENTICAL html to rendering the original (Decimal-bearing)
    model directly. Proves spec_json can be persisted (Decimal -> str, never through
    float) without corrupting a future re-render of that stored spec."""
    from app.services.report.report_html import render_report_html

    spec = {
        "title": "Income Statement — Jun 2026",
        "sections": [{"type": "financial_statement", "model": is_model}],
        "provenance": {"sources": []},
    }
    direct_html = render_report_html(spec)

    safe_model = statement_model_json_safe(is_model)
    round_tripped = json.loads(json.dumps(safe_model))  # the actual persistence round trip
    restored_model = statement_model_restore_decimals(round_tripped)
    roundtrip_spec = {
        "title": "Income Statement — Jun 2026",
        "sections": [{"type": "financial_statement", "model": restored_model}],
        "provenance": {"sources": []},
    }
    roundtrip_html = render_report_html(roundtrip_spec)

    assert roundtrip_html == direct_html


# ===========================================================================
# Task 5 — offline rendered-artifact preview harness (no DB, no network, no LLM)
# ===========================================================================


def test_render_statement_preview_harness_writes_four_files(tmp_path):
    """``scripts/render_statement_preview.py`` is the controller's eyeball gate for the
    redesign (``.claude/rules/report-design.md`` #2): it must produce all four rendered
    artifacts offline and each must contain the statement-defining string the renderer
    actually emits for that statement type -- not a recomputation, the exact literal
    text asserted in test_report_playbooks.py's compose tests."""
    from scripts.render_statement_preview import main

    written = main(["--out-dir", str(tmp_path)])
    names = {p.name for p in written}
    assert names == {
        "income_statement.html",
        "balance_sheet.html",
        "trial_balance.html",
        "income_statement_degraded.html",
    }
    for path in written:
        assert path.exists()

    contents = {p.name: p.read_text(encoding="utf-8") for p in written}
    assert "Net income" in contents["income_statement.html"]  # KPI card label
    assert "Assets = Liabilities + Equity" in contents["balance_sheet.html"]  # BS check label
    assert "Debits = Credits" in contents["trial_balance.html"]  # TB check label
    # the degraded variant still renders the statement (r1 never degrades)...
    assert "Net income" in contents["income_statement_degraded.html"]
    # ...but with the compare-fed chip/deltas actually absent (proves it degraded,
    # not that it silently rendered the same happy-path fixture twice).
    assert "vs May 2026" not in contents["income_statement_degraded.html"]


# ===========================================================================
# T2 gate round 3, F-1 — a net-negative-revenue period must not defeat the materiality
# gates. threshold_dollars must be based on abs(revenue), never revenue's raw sign.
# ===========================================================================


def test_negative_revenue_account_mover_gate_still_discriminates():
    model = build_statement_model(fx.income_statement_section(), fx.negative_revenue_mover_payloads())
    watch_texts = [w["text"] for w in model["watch"]]
    assert not any("Immaterial Mover" in t for t in watch_texts), (
        f"an immaterial ($300, 0.3% of |revenue|) mover must stay silent, got: {watch_texts}"
    )
    assert any("Material Mover" in t for t in watch_texts), (
        f"a material ($2000, 2% of |revenue|) mover must fire, got: {watch_texts}"
    )


def test_negative_revenue_highlight_gate_still_discriminates():
    model = build_statement_model(fx.income_statement_section(), fx.negative_revenue_mover_payloads())
    highlights = model["highlights"]
    assert not any("Immaterial Mover" in h for h in highlights), (
        f"an immaterial ($300, 0.3% of |revenue|) mover must not drive a highlight, got: {highlights}"
    )
    assert any("Material Mover" in h for h in highlights), (
        f"a material ($2000, 2% of |revenue|) mover must drive the OpEx highlight, got: {highlights}"
    )


def test_negative_revenue_only_immaterial_mover_no_highlight_fires():
    """T2 gate F-1 regression (round-3 reviewer's recommended discriminating test): with
    the ONLY mover in the whole statement being immaterial (0.2% of |revenue|, under the
    0.5% highlights threshold), NO highlight may fire. Unlike
    ``test_negative_revenue_highlight_gate_still_discriminates`` above (which always has
    a genuinely material mover present, so ``largest_mover()`` picks it regardless of
    whether the threshold gate itself is broken -- that test would pass even with a
    sabotaged abs()), THIS fixture has no material mover to hide behind: a sabotaged
    abs(revenue) (reverted to bare ``revenue``) flips this negative-revenue threshold
    negative, and the immaterial mover would incorrectly clear it and fire a highlight."""
    model = build_statement_model(fx.income_statement_section(), fx.negative_revenue_only_immaterial_mover_payloads())
    assert model["highlights"] == []


# ===========================================================================
# T2 gate round 3, F-2 — a trend/compare source landing AT the row cap is invisible to
# the truncated flag (the SQL cap is baked into the query text, so totalResults reflects
# the CAPPED count) -- must degrade rather than render as if nothing were wrong.
# ===========================================================================


def test_trend_at_cap_degrades_entirely_with_specific_chip():
    model = build_statement_model(fx.income_statement_section(), fx.trend_row_cap_boundary_payloads(STATEMENT_ROW_CAP))
    assert model["trend"] is None
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["spark"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "trend source at row cap — trend omitted" in watch_texts
    # the specific chip replaces the generic one -- never both for the same reason
    assert "Trend comparison unavailable this run" not in watch_texts


def test_trend_just_under_cap_renders_normally():
    model = build_statement_model(
        fx.income_statement_section(), fx.trend_row_cap_boundary_payloads(STATEMENT_ROW_CAP - 1)
    )
    assert model["trend"] is not None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "trend source at row cap — trend omitted" not in watch_texts


def test_prior_at_cap_degrades_with_existing_chip():
    model = build_statement_model(fx.income_statement_section(), fx.prior_at_cap_payloads(STATEMENT_ROW_CAP))
    assert model["prior_period"] is None
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["mom_delta"] is None
    watch_texts = [w["text"] for w in model["watch"]]
    assert "Prior-period comparison unavailable this run" in watch_texts


def test_prior_just_under_cap_renders_normally():
    model = build_statement_model(fx.income_statement_section(), fx.prior_at_cap_payloads(STATEMENT_ROW_CAP - 1))
    assert model["prior_period"] is not None


def test_r1_at_cap_still_only_warns_unaffected_by_f2():
    """F-2 only changes COMPARE-rid at-cap handling; r1's existing wave-3/4 at-cap warn
    (never a degrade/fail) must be completely unchanged."""
    model = build_statement_model(fx.income_statement_section(), fx.row_cap_boundary_payloads(STATEMENT_ROW_CAP))
    cap_items = [w for w in model["watch"] if "row cap reached" in w["text"]]
    assert len(cap_items) == 1


# ===========================================================================
# T2 gate round 3, F-3 — the prior CELL's parens (reduces_profit) convention must key
# off the PRIOR amount's own sign, not the current amount's.
# ===========================================================================


def test_prior_cell_reduces_profit_keyed_to_its_own_sign():
    model = build_statement_model(fx.income_statement_section(), fx.sign_flipping_contra_revenue_payloads())
    section = next(s for s in model["sections"] if s["key"] == "1-Revenue")
    account = next(a for a in section["accounts"] if a["number"] == "4099")
    assert account["current"] == "$5,000"  # positive current -- natural sign, no parens
    assert account["prior"] == "($3,000)", (
        f"prior was -$3,000 (contra) -- must render in parens per its OWN sign, got: {account['prior']!r}"
    )
