"""Tests for auto-generating charts from NetSuite financial reports."""


from app.schemas.chart import ChartData
from app.services.chat.financial_chart_builder import build_financial_chart

INCOME_TREND_SUMMARY = {
    "by_period": {
        "Jan 2026": {
            "total_revenue": 15639099.87,
            "total_other_income": 0.0,
            "total_cogs": 11711447.35,
            "gross_profit": 3927652.52,
            "total_operating_expense": 2667823.43,
            "operating_income": 1259829.09,
            "total_other_expense": 63594.5,
            "net_income": 1196234.59,
        },
        "Feb 2026": {
            "total_revenue": 12441035.23,
            "total_other_income": 0.0,
            "total_cogs": 9712796.05,
            "gross_profit": 2728239.18,
            "total_operating_expense": 2691708.16,
            "operating_income": 36531.02,
            "total_other_expense": -21081.31,
            "net_income": 57612.33,
        },
    }
}

INCOME_SINGLE_SUMMARY = {
    "total_revenue": 15639099.87,
    "total_cogs": 11711447.35,
    "gross_profit": 3927652.52,
    "total_operating_expense": 2667823.43,
    "operating_income": 1259829.09,
    "net_income": 1196234.59,
}

BALANCE_TREND_SUMMARY = {
    "by_period": {
        "Jan 2026": {
            "total_assets": 50000000.0,
            "total_liabilities": 30000000.0,
            "total_equity": 20000000.0,
        },
        "Feb 2026": {
            "total_assets": 52000000.0,
            "total_liabilities": 31000000.0,
            "total_equity": 21000000.0,
        },
    }
}


class TestBuildFinancialChart:

    def test_income_trend_returns_chart(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        assert chart is not None
        assert isinstance(chart, ChartData)

    def test_income_trend_chart_type_bar(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        assert chart.chart_type == "bar"

    def test_income_trend_has_periods_as_x_axis(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        assert chart.x_axis.key == "period"
        assert len(chart.data) == 2  # Jan + Feb

    def test_income_trend_has_revenue_cogs_opex_series(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        y_keys = {y.key for y in chart.y_axes}
        assert "revenue" in y_keys
        assert "cogs" in y_keys
        assert "operating_expenses" in y_keys

    def test_income_trend_data_values_correct(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        jan = next(d for d in chart.data if d["period"] == "Jan 2026")
        assert jan["revenue"] == 15639099.87
        assert jan["cogs"] == 11711447.35

    def test_income_trend_has_net_income(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        y_keys = {y.key for y in chart.y_axes}
        assert "net_income" in y_keys

    def test_income_trend_title(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        assert "Income Statement" in chart.title

    def test_single_period_returns_none(self):
        """Single-period reports don't need trend charts."""
        chart = build_financial_chart(
            report_type="income_statement",
            summary=INCOME_SINGLE_SUMMARY,
        )
        assert chart is None

    def test_balance_trend_returns_chart(self):
        chart = build_financial_chart(
            report_type="balance_sheet_trend",
            summary=BALANCE_TREND_SUMMARY,
        )
        assert chart is not None

    def test_balance_trend_has_assets_liabilities_equity(self):
        chart = build_financial_chart(
            report_type="balance_sheet_trend",
            summary=BALANCE_TREND_SUMMARY,
        )
        y_keys = {y.key for y in chart.y_axes}
        assert "assets" in y_keys
        assert "liabilities" in y_keys
        assert "equity" in y_keys

    def test_balance_trend_title(self):
        chart = build_financial_chart(
            report_type="balance_sheet_trend",
            summary=BALANCE_TREND_SUMMARY,
        )
        assert "Balance Sheet" in chart.title

    def test_malformed_summary_returns_none(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary={},
        )
        assert chart is None

    def test_empty_by_period_returns_none(self):
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary={"by_period": {}},
        )
        assert chart is None

    def test_single_period_in_by_period_returns_none(self):
        """Only 1 period -- no trend to show."""
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary={"by_period": {"Jan 2026": {"total_revenue": 100}}},
        )
        assert chart is None

    def test_unknown_report_type_returns_none(self):
        chart = build_financial_chart(
            report_type="trial_balance",
            summary=INCOME_TREND_SUMMARY,
        )
        assert chart is None

    def test_chart_data_matches_schema(self):
        """Verify output matches ChartData Pydantic model exactly."""
        chart = build_financial_chart(
            report_type="income_statement_trend",
            summary=INCOME_TREND_SUMMARY,
        )
        # Should serialize without error
        chart_dict = chart.model_dump()
        assert "chart_type" in chart_dict
        assert "x_axis" in chart_dict
        assert "y_axes" in chart_dict
        assert "data" in chart_dict
