"""Tests for tool_choice integration in the orchestrator."""


def test_build_financial_mode_task():
    """Financial mode task should reference netsuite_financial_report."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    result = _build_financial_mode_task("Show me the income statement for Feb 2026")
    assert isinstance(result, str)
    assert "netsuite_financial_report" in result or "FINANCIAL REPORT" in result
    assert "Show me the income statement" in result


def test_build_financial_mode_task_has_parameters():
    """Financial mode task should include parameter guidance."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    result = _build_financial_mode_task("Income statement for Q1")
    assert "report_type" in result or "income_statement" in result
