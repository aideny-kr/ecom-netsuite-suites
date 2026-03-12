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
