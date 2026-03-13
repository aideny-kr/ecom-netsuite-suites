"""Tests for tool_choice integration in the orchestrator."""


def test_build_financial_mode_task_mcp():
    """use_mcp=True should reference ns_runReport."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    result = _build_financial_mode_task("Show me the income statement for Feb 2026", use_mcp=True)
    assert isinstance(result, str)
    assert "ns_runReport" in result
    assert "netsuite_financial_report" not in result or "FALLBACK" in result


def test_build_financial_mode_task_local():
    """use_mcp=False should reference netsuite_financial_report."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    result = _build_financial_mode_task("Show me the income statement for Feb 2026", use_mcp=False)
    assert isinstance(result, str)
    assert "netsuite_financial_report" in result


def test_build_financial_mode_task_default_is_mcp():
    """Default should be use_mcp=True."""
    from app.services.chat.orchestrator import _build_financial_mode_task

    result = _build_financial_mode_task("Show me the income statement")
    assert "ns_runReport" in result
