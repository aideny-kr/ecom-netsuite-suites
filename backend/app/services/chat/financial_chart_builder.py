"""Auto-generate chart specs from NetSuite financial report data.

Deterministic post-processing -- no LLM calls. Takes financial report
summary data and builds ChartData specs for the frontend chart renderer.

Only generates charts for trend reports (2+ periods). Single-period
reports don't need trend visualization.
"""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.chart import ChartAxis, ChartData, ChartOptions

logger = logging.getLogger(__name__)


def build_financial_chart(
    report_type: str,
    summary: dict[str, Any],
) -> ChartData | None:
    """Build a chart spec from financial report summary data.

    Returns None if:
    - Report type not supported (only income_statement_trend, balance_sheet_trend)
    - Summary missing or has < 2 periods
    - Any parsing error (graceful degradation)
    """
    try:
        by_period = summary.get("by_period")
        if not by_period or len(by_period) < 2:
            return None

        periods = list(by_period.keys())

        if report_type in ("income_statement_trend", "income_statement"):
            if "by_period" not in summary:
                return None
            return _build_income_chart(periods, by_period)
        elif report_type in ("balance_sheet_trend", "balance_sheet"):
            if "by_period" not in summary:
                return None
            return _build_balance_chart(periods, by_period)
        else:
            return None
    except Exception as exc:
        logger.warning("financial_chart_builder.failed: %s", str(exc)[:100])
        return None


def _build_income_chart(periods: list[str], by_period: dict) -> ChartData:
    """Income statement trend -> grouped bar chart with net income."""
    data = []
    for period in periods:
        p = by_period[period]
        data.append({
            "period": period,
            "revenue": p.get("total_revenue", 0),
            "cogs": p.get("total_cogs", 0),
            "operating_expenses": p.get("total_operating_expense", 0),
            "net_income": p.get("net_income", 0),
        })

    return ChartData(
        chart_type="bar",
        title="Income Statement Trend",
        subtitle=f"{periods[0]} \u2014 {periods[-1]}",
        x_axis=ChartAxis(label="Period", key="period"),
        y_axes=[
            ChartAxis(label="Revenue", key="revenue", color="#6366f1"),
            ChartAxis(label="COGS", key="cogs", color="#ef4444"),
            ChartAxis(label="Operating Expenses", key="operating_expenses", color="#f59e0b"),
            ChartAxis(label="Net Income", key="net_income", color="#10b981"),
        ],
        data=data,
        options=ChartOptions(show_legend=True),
    )


def _build_balance_chart(periods: list[str], by_period: dict) -> ChartData:
    """Balance sheet trend -> grouped bar chart."""
    data = []
    for period in periods:
        p = by_period[period]
        data.append({
            "period": period,
            "assets": p.get("total_assets", 0),
            "liabilities": p.get("total_liabilities", 0),
            "equity": p.get("total_equity", 0),
        })

    return ChartData(
        chart_type="bar",
        title="Balance Sheet Trend",
        subtitle=f"{periods[0]} \u2014 {periods[-1]}",
        x_axis=ChartAxis(label="Period", key="period"),
        y_axes=[
            ChartAxis(label="Assets", key="assets", color="#6366f1"),
            ChartAxis(label="Liabilities", key="liabilities", color="#ef4444"),
            ChartAxis(label="Equity", key="equity", color="#10b981"),
        ],
        data=data,
        options=ChartOptions(show_legend=True),
    )
