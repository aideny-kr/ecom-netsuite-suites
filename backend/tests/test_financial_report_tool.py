"""Tests for the financial report template registry."""

import pytest


def test_registry_has_required_report_types():
    from app.mcp.tools.netsuite_financial_report import REPORT_TEMPLATES

    assert "income_statement" in REPORT_TEMPLATES
    assert "balance_sheet" in REPORT_TEMPLATES
    assert "trial_balance" in REPORT_TEMPLATES
    assert "income_statement_trend" in REPORT_TEMPLATES
    assert "balance_sheet_trend" in REPORT_TEMPLATES


def test_each_template_has_required_fields():
    from app.mcp.tools.netsuite_financial_report import REPORT_TEMPLATES

    for name, tmpl in REPORT_TEMPLATES.items():
        assert "description" in tmpl, f"{name} missing description"
        assert "sql_template" in tmpl, f"{name} missing sql_template"
        assert "period_mode" in tmpl, f"{name} missing period_mode"
        assert tmpl["period_mode"] in ("single_period", "inception_to_date", "multi_period"), (
            f"{name} has invalid period_mode: {tmpl['period_mode']}"
        )


def test_income_statement_template_has_mandatory_filters():
    from app.mcp.tools.netsuite_financial_report import REPORT_TEMPLATES

    sql = REPORT_TEMPLATES["income_statement"]["sql_template"]
    assert "tal.posting = 'T'" in sql or "t.posting = 'T'" in sql
    assert "accountingbook" in sql.lower()
    assert "isprimary" in sql.lower()
    assert "isquarter = 'F'" in sql
    assert "isyear = 'F'" in sql
    assert "accttype" in sql.lower()
    assert "{period_filter}" in sql  # Parameterized placeholder


def test_balance_sheet_template_no_start_date():
    """Balance sheet = inception-to-date. Template must NOT have a start date filter."""
    from app.mcp.tools.netsuite_financial_report import REPORT_TEMPLATES

    sql = REPORT_TEMPLATES["balance_sheet"]["sql_template"]
    assert "ap.enddate <=" in sql or "{period_filter}" in sql
    # Must NOT have ap.startdate >= in the template
    assert "ap.startdate >=" not in sql.replace("{period_filter}", "")


def test_trend_templates_include_periodname_column():
    """Trend reports must include ap.periodname in SELECT and GROUP BY for period breakdown."""
    from app.mcp.tools.netsuite_financial_report import REPORT_TEMPLATES

    for name in ("income_statement_trend", "balance_sheet_trend"):
        sql = REPORT_TEMPLATES[name]["sql_template"]
        assert "ap.periodname" in sql or "ap_period.periodname" in sql, f"{name} missing periodname in SELECT"
        assert "ap.startdate" in sql or "ap_period.startdate" in sql, f"{name} missing startdate for ordering"


def test_trend_templates_use_multi_period_mode():
    from app.mcp.tools.netsuite_financial_report import REPORT_TEMPLATES

    assert REPORT_TEMPLATES["income_statement_trend"]["period_mode"] == "multi_period"
    assert REPORT_TEMPLATES["balance_sheet_trend"]["period_mode"] == "multi_period"


# --- Period filter builder tests ---


def test_build_period_filter_single_month():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    result = build_period_filter("single_period", "Feb 2026")
    assert result == "ap.periodname = 'Feb 2026'"


def test_build_period_filter_multi_month():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    result = build_period_filter("single_period", "Jan 2026, Feb 2026, Mar 2026")
    assert result == "ap.periodname IN ('Jan 2026', 'Feb 2026', 'Mar 2026')"


def test_build_period_filter_inception_to_date():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    result = build_period_filter("inception_to_date", "Feb 2026")
    assert "ap.enddate <=" in result
    assert "ap.startdate" not in result


def test_build_period_filter_inception_to_date_with_explicit_date():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    result = build_period_filter("inception_to_date", "2026-01-31")
    assert "ap.enddate <= TO_DATE('2026-01-31'" in result


def test_build_period_filter_multi_period():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    result = build_period_filter("multi_period", "Jan 2026, Feb 2026, Mar 2026")
    assert result == "ap.periodname IN ('Jan 2026', 'Feb 2026', 'Mar 2026')"


def test_build_period_filter_multi_period_single():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    result = build_period_filter("multi_period", "Jan 2026")
    assert result == "ap.periodname = 'Jan 2026'"


def test_build_period_filter_rejects_sql_injection():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    with pytest.raises(ValueError, match="Invalid period"):
        build_period_filter("single_period", "'; DROP TABLE account; --")


def test_build_period_filter_rejects_empty():
    from app.mcp.tools.netsuite_financial_report import build_period_filter

    with pytest.raises(ValueError, match="Period.*required"):
        build_period_filter("single_period", "")


# --- Execute function tests ---

from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_execute_income_statement_delegates_to_suiteql():
    """execute() should build SQL from template + period, then call netsuite_suiteql.execute()."""
    from app.mcp.tools.netsuite_financial_report import execute

    mock_suiteql_result = {
        "success": True,
        "columns": ["acctnumber", "acctname", "accttype", "section", "amount"],
        "items": [
            {"acctnumber": "4000", "acctname": "Revenue", "accttype": "Income", "section": "1-Revenue", "amount": 100000},
        ],
        "total_rows": 1,
    }

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_suiteql_result

        result = await execute(
            report_type="income_statement",
            period="Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    assert result["success"] is True
    assert result["report_type"] == "income_statement"
    assert result["period"] == "Feb 2026"
    assert "items" in result

    called_query = mock_exec.call_args[1]["query"]
    assert "ap.periodname = 'Feb 2026'" in called_query
    assert "{period_filter}" not in called_query


@pytest.mark.asyncio
async def test_execute_balance_sheet_uses_inception_to_date():
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"success": True, "columns": [], "items": [], "total_rows": 0}

        await execute(
            report_type="balance_sheet",
            period="Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    called_query = mock_exec.call_args[1]["query"]
    assert "ap.enddate <=" in called_query
    assert "ap.startdate >=" not in called_query


@pytest.mark.asyncio
async def test_execute_trend_uses_multi_period():
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"success": True, "columns": [], "items": [], "total_rows": 0}

        await execute(
            report_type="income_statement_trend",
            period="Jan 2026, Feb 2026, Mar 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    called_query = mock_exec.call_args[1]["query"]
    assert "ap.periodname IN ('Jan 2026', 'Feb 2026', 'Mar 2026')" in called_query
    assert "ap.periodname" in called_query  # In SELECT for grouping


@pytest.mark.asyncio
async def test_execute_with_subsidiary_filter():
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"success": True, "columns": [], "items": [], "total_rows": 0}

        await execute(
            report_type="income_statement",
            period="Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
            subsidiary_id=3,
        )

    called_query = mock_exec.call_args[1]["query"]
    assert "t.subsidiary = 3" in called_query


@pytest.mark.asyncio
async def test_execute_invalid_report_type():
    from app.mcp.tools.netsuite_financial_report import execute

    result = await execute(
        report_type="cash_flow",
        period="Feb 2026",
        tenant_id="test-tenant",
        db=AsyncMock(),
    )

    assert result["success"] is False
    assert "Unknown report type" in result["error"]


@pytest.mark.asyncio
async def test_execute_invalid_period():
    from app.mcp.tools.netsuite_financial_report import execute

    result = await execute(
        report_type="income_statement",
        period="'; DROP TABLE --",
        tenant_id="test-tenant",
        db=AsyncMock(),
    )

    assert result["success"] is False
    assert "Invalid period" in result["error"]


@pytest.mark.asyncio
async def test_execute_handles_suiteql_exception():
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.side_effect = Exception("Connection timeout")

        result = await execute(
            report_type="income_statement",
            period="Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    assert result["success"] is False
    assert "Connection timeout" in result["error"]


@pytest.mark.asyncio
async def test_execute_propagates_error_message_not_boolean():
    """When suiteql returns {"error": True, "message": "..."}, execute() should surface the message string."""
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"error": True, "message": "No active NetSuite connection found for this tenant."}

        result = await execute(
            report_type="income_statement",
            period="Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    assert result["success"] is False
    assert isinstance(result["error"], str)
    assert "No active NetSuite connection" in result["error"]
    assert result["error"] is not True  # Must not be boolean


@pytest.mark.asyncio
async def test_execute_maps_rows_key_to_items():
    """SuiteQL returns 'rows'/'row_count' but financial report exposes 'items'/'total_rows'."""
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {
            "columns": ["acctnumber", "acctname", "amount"],
            "rows": [["4000", "Revenue", 100000]],
            "row_count": 1,
            "truncated": False,
        }

        result = await execute(
            report_type="income_statement",
            period="Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    assert result["success"] is True
    # Rows are normalized from list-of-lists to list-of-dicts
    assert result["items"] == [{"acctnumber": "4000", "acctname": "Revenue", "amount": 100000}]
    assert result["total_rows"] == 1
    assert "summary" in result


# --- Per-period trend summary tests ---


def test_compute_summary_income_trend_groups_by_period():
    from app.mcp.tools.netsuite_financial_report import _compute_summary

    rows = [
        {"periodname": "Jan 2026", "section": "1-Revenue", "amount": 5000},
        {"periodname": "Jan 2026", "section": "3-COGS", "amount": 2000},
        {"periodname": "Jan 2026", "section": "4-Operating Expense", "amount": 1000},
        {"periodname": "Feb 2026", "section": "1-Revenue", "amount": 8000},
        {"periodname": "Feb 2026", "section": "3-COGS", "amount": 3000},
        {"periodname": "Feb 2026", "section": "4-Operating Expense", "amount": 1500},
    ]
    summary = _compute_summary("income_statement_trend", rows)
    assert "by_period" in summary
    assert "Jan 2026" in summary["by_period"]
    assert "Feb 2026" in summary["by_period"]

    jan = summary["by_period"]["Jan 2026"]
    assert jan["total_revenue"] == 5000
    assert jan["total_cogs"] == 2000
    assert jan["gross_profit"] == 3000
    assert jan["total_operating_expense"] == 1000
    assert jan["net_income"] == 2000  # 5000 - 2000 - 1000

    feb = summary["by_period"]["Feb 2026"]
    assert feb["total_revenue"] == 8000
    assert feb["total_cogs"] == 3000
    assert feb["gross_profit"] == 5000
    assert feb["net_income"] == 3500  # 8000 - 3000 - 1500


def test_compute_summary_single_period_is_flat():
    """Single-period income statement should return flat dict (no by_period)."""
    from app.mcp.tools.netsuite_financial_report import _compute_summary

    rows = [
        {"section": "1-Revenue", "amount": 10000},
        {"section": "3-COGS", "amount": 4000},
    ]
    summary = _compute_summary("income_statement", rows)
    assert "by_period" not in summary
    assert summary["total_revenue"] == 10000
    assert summary["total_cogs"] == 4000
    assert summary["net_income"] == 6000


def test_compute_summary_balance_sheet_trend_groups_by_period():
    from app.mcp.tools.netsuite_financial_report import _compute_summary

    rows = [
        {"periodname": "Jan 2026", "section": "1-Assets", "balance": 50000},
        {"periodname": "Jan 2026", "section": "2-Liabilities", "balance": 20000},
        {"periodname": "Jan 2026", "section": "3-Equity", "balance": 30000},
        {"periodname": "Feb 2026", "section": "1-Assets", "balance": 55000},
        {"periodname": "Feb 2026", "section": "2-Liabilities", "balance": 22000},
        {"periodname": "Feb 2026", "section": "3-Equity", "balance": 33000},
    ]
    summary = _compute_summary("balance_sheet_trend", rows)
    assert "by_period" in summary

    jan = summary["by_period"]["Jan 2026"]
    assert jan["total_assets"] == 50000
    assert jan["total_liabilities"] == 20000
    assert jan["total_equity"] == 30000
    assert jan["liabilities_plus_equity"] == 50000

    feb = summary["by_period"]["Feb 2026"]
    assert feb["total_assets"] == 55000


@pytest.mark.asyncio
async def test_execute_trend_returns_per_period_summary():
    """Trend reports should return summary.by_period with per-month breakdowns."""
    from app.mcp.tools.netsuite_financial_report import execute

    with patch("app.mcp.tools.netsuite_financial_report._execute_suiteql", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {
            "columns": ["periodname", "startdate", "acctnumber", "acctname", "accttype", "section", "amount"],
            "rows": [
                ["Jan 2026", "2026-01-01", "4000", "Revenue", "Income", "1-Revenue", 5000],
                ["Jan 2026", "2026-01-01", "5000", "COGS", "COGS", "3-COGS", 2000],
                ["Feb 2026", "2026-02-01", "4000", "Revenue", "Income", "1-Revenue", 8000],
                ["Feb 2026", "2026-02-01", "5000", "COGS", "COGS", "3-COGS", 3000],
            ],
            "row_count": 4,
        }

        result = await execute(
            report_type="income_statement_trend",
            period="Jan 2026, Feb 2026",
            tenant_id="test-tenant",
            db=AsyncMock(),
        )

    assert result["success"] is True
    assert "by_period" in result["summary"]
    assert result["summary"]["by_period"]["Jan 2026"]["total_revenue"] == 5000
    assert result["summary"]["by_period"]["Jan 2026"]["net_income"] == 3000
    assert result["summary"]["by_period"]["Feb 2026"]["total_revenue"] == 8000
    assert result["summary"]["by_period"]["Feb 2026"]["net_income"] == 5000


# --- MCP registry + allowed tools tests ---


def test_tool_registered_in_mcp_registry():
    from app.mcp.registry import TOOL_REGISTRY

    assert "netsuite.financial_report" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["netsuite.financial_report"]
    assert "execute" in entry
    assert "params_schema" in entry
    assert "report_type" in entry["params_schema"]
    assert "period" in entry["params_schema"]


def test_tool_in_allowed_chat_tools():
    from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

    assert "netsuite.financial_report" in ALLOWED_CHAT_TOOLS


# --- Orchestrator integration tests ---


def test_financial_mode_prompt_references_tool():
    """The financial mode augmentation should instruct agent to use the tool, not raw SQL."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    task = _build_financial_mode_task("Show me the income statement for February 2026")
    assert "ns_runReport" in task or "netsuite_financial_report" in task or "netsuite.report" in task
    assert "report" in task.lower()
    # Should NOT contain raw SQL templates
    assert "transactionaccountingline" not in task


def test_financial_mode_prompt_mentions_trend():
    from app.services.chat.orchestrator import _build_financial_mode_task

    task = _build_financial_mode_task("Show revenue trend Q1 2026")
    # The task should reference the report tool and preserve the user's query
    assert "ns_runReport" in task or "netsuite_financial_report" in task or "FINANCIAL" in task
    assert "trend" in task.lower()


# --- Intent parser tests ---


def test_parse_income_statement_explicit_month():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent

    result = parse_report_intent("Show me the income statement for February 2026")
    assert result is not None
    assert result["report_type"] == "income_statement"
    assert result["period"] == "Feb 2026"


def test_parse_balance_sheet():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent

    result = parse_report_intent("Pull the balance sheet for March 2026")
    assert result is not None
    assert result["report_type"] == "balance_sheet"
    assert result["period"] == "Mar 2026"


def test_parse_last_month():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent
    from datetime import datetime, timedelta

    result = parse_report_intent("Show me the income statement for last month")
    assert result is not None
    assert result["report_type"] == "income_statement"
    # Should resolve to previous month
    now = datetime.utcnow()
    first = now.replace(day=1)
    last = first - timedelta(days=1)
    expected_month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][last.month - 1]
    assert expected_month in result["period"]


def test_parse_quarter():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent

    result = parse_report_intent("P&L for Q1 2026")
    assert result is not None
    assert result["report_type"] == "income_statement_trend"
    assert result["period"] == "Jan 2026, Feb 2026, Mar 2026"


def test_parse_trend_multi_month():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent

    result = parse_report_intent("Show revenue trend Jan 2026 Feb 2026 Mar 2026")
    assert result is not None
    assert result["report_type"] == "income_statement_trend"
    assert "Jan 2026" in result["period"]
    assert "Feb 2026" in result["period"]
    assert "Mar 2026" in result["period"]


def test_parse_no_period_returns_none():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent

    result = parse_report_intent("Show me the income statement")
    assert result is None


def test_parse_last_quarter():
    from app.mcp.tools.netsuite_financial_report import parse_report_intent

    result = parse_report_intent("Show me income statement for last quarter")
    assert result is not None
    assert result["report_type"] == "income_statement_trend"
    # Should have 3 months
    assert len(result["period"].split(", ")) == 3
