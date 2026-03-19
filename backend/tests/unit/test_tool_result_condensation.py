"""TDD: Condense tool results in conversation history to reduce token bloat.

Tool results (SuiteQL data, file contents) replay verbatim in every
subsequent turn. A 100-row SuiteQL result adds ~12K chars to every
follow-up message. Condensation replaces these with short summaries
for older messages while keeping recent results intact.
"""

import json

import pytest

from app.services.chat.history_compactor import condense_tool_results


class TestCondenseToolResults:
    def test_large_suiteql_result_condensed(self):
        """A large SuiteQL result (20+ rows) should be condensed to a summary."""
        rows = [[f"SO-{i}", f"2026-01-{i:02d}", f"Customer {i}", f"{i * 100}"] for i in range(1, 21)]
        result = json.dumps({
            "columns": ["tranid", "trandate", "customer", "total"],
            "rows": rows,
            "row_count": 20,
        })
        content = f"Here are the results.\n\n```json\n{result}\n```"

        condensed = condense_tool_results(content, max_result_chars=500)

        assert len(condensed) < len(content)
        assert "20" in condensed or "rows" in condensed.lower()
        assert "tranid" in condensed or "columns" in condensed.lower()

    def test_small_result_unchanged(self):
        """Results under max_result_chars should pass through unchanged."""
        content = "Found 2 orders: SO-123 and SO-456."

        condensed = condense_tool_results(content, max_result_chars=500)

        assert condensed == content

    def test_no_tool_results_unchanged(self):
        """Messages with no tool results should pass through unchanged."""
        content = "The RMA workflow in NetSuite works as follows..."

        condensed = condense_tool_results(content, max_result_chars=500)

        assert condensed == content

    def test_large_json_block_condensed(self):
        """A large JSON block embedded in content should be condensed."""
        data = {"data": [{"id": i, "name": f"item_{i}"} for i in range(50)]}
        json_str = json.dumps(data)
        content = f"Query results:\n{json_str}\n\nThese are the items."

        condensed = condense_tool_results(content, max_result_chars=500)

        assert len(condensed) < len(content)
        # Should preserve the non-JSON text
        assert "Query results" in condensed
        assert "items" in condensed

    def test_multiple_json_blocks_all_condensed(self):
        """Multiple large JSON blocks should all be condensed."""
        block1 = json.dumps({"rows": [[i, f"name_{i}", f"desc_{i}"] for i in range(50)], "columns": ["id", "name", "desc"]})
        block2 = json.dumps({"data": [{"x": i, "y": f"val_{i}", "z": f"long_description_{i}"} for i in range(50)]})
        content = f"First:\n{block1}\n\nSecond:\n{block2}"

        condensed = condense_tool_results(content, max_result_chars=300)

        assert len(condensed) < len(content)


class TestHistoryCondensation:
    def test_recent_messages_not_condensed(self):
        """The last 2 messages should never be condensed."""
        from app.services.chat.history_compactor import build_condensed_history

        big_json = json.dumps({"rows": [[i, f"data_{i}"] for i in range(50)], "columns": ["id", "data"], "row_count": 50})
        messages = [
            {"role": "user", "content": "show me 100 orders"},
            {"role": "assistant", "content": f"Results:\n{big_json}"},  # Large JSON
            {"role": "user", "content": "now filter by status"},
            {"role": "assistant", "content": f"Filtered:\n{big_json}"},  # Large — but recent
        ]

        result = build_condensed_history(messages, keep_recent=2)

        # Last 2 messages (index 2,3) should be full
        assert "Filtered" in result[-1]["content"]
        assert big_json in result[-1]["content"]  # Recent — NOT condensed
        assert result[-2]["content"] == "now filter by status"
        # First assistant message (index 1) should be condensed
        assert len(result[1]["content"]) < len(big_json)

    def test_user_messages_never_condensed(self):
        """User messages should never be condensed regardless of position."""
        from app.services.chat.history_compactor import build_condensed_history

        messages = [
            {"role": "user", "content": "a long user message " * 200},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "another question"},
            {"role": "assistant", "content": "another response"},
        ]

        result = build_condensed_history(messages, keep_recent=2)

        # User message at index 0 should be unchanged
        assert result[0]["content"] == "a long user message " * 200
