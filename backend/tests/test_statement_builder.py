"""Pure Decimal statement model builder — turns resolved financial_statement report
payloads into the render-ready MODEL Task 3's renderer consumes verbatim.

No DB, no network, no LLM anywhere in this file — every input is a hand-computed fixture
from tests.fixtures.statement_fixture, and every expected total/string is a hand-checked
constant from that same module (never a recomputation of the code under test).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.report.statement_builder import (
    build_statement_model,
    fmt_money,
    fmt_money_delta,
    fmt_pct,
    fmt_pct_delta,
    fmt_pp,
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
    assert model["watch"] == []  # every watch rule needs a compare source
    assert model["highlights"] == []


def test_is_failed_compare_source_degrades_like_missing():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_failed_compare())
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["mom_delta"] is None
    assert kpis["revenue"]["mom_pct"] is None
    assert model["prior_period"] is None


def test_malformed_r1_raises_value_error():
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), fx.malformed_r1_payload())


def test_missing_r1_entirely_raises_value_error():
    with pytest.raises(ValueError):
        build_statement_model(fx.income_statement_section(), {})


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


def test_tb_kpis_no_mom(tb_model):
    kpis = {k["key"]: k for k in tb_model["kpis"]}
    assert kpis["total_debits"]["value"] == "$1,600,000"
    assert kpis["total_credits"]["value"] == "$1,600,000"
    # brief: TB KPIs are flat totals + in-balance check, no MoM comparison
    assert kpis["total_debits"]["mom_delta"] is None
    assert kpis["total_credits"]["mom_delta"] is None


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


# ===========================================================================
# Unknown statement type — malformed section input
# ===========================================================================


def test_unknown_statement_type_raises_value_error():
    section = fx.income_statement_section()
    section["statement"] = "cash_flow"  # not one of the three supported types
    with pytest.raises(ValueError):
        build_statement_model(section, fx.income_statement_payloads())
