"""Tests for tool_choice integration in the orchestrator."""


def test_build_financial_mode_task_returns_string():
    """_build_financial_mode_task should return a task string with tool instructions."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    result = _build_financial_mode_task("Show me the income statement for Feb 2026")
    assert isinstance(result, str)
    assert "netsuite.financial_report" in result or "netsuite_financial_report" in result
