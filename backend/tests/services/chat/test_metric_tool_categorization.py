# backend/tests/services/chat/test_metric_tool_categorization.py
from app.services.chat.tool_categories import categorize


def test_metric_compute_is_data_table():
    assert categorize("metric_compute") == "data_table"
    assert categorize("metric.compute") == "data_table"


def test_metric_resolve_is_not_intercepted():
    # resolve returns definitions the LLM must read; it must NOT be stripped.
    assert categorize("metric_resolve") == "other"
