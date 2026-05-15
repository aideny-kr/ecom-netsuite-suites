"""Regression tests for pricing task output no-LLM-number persistence.

Pricing prices must come from the deterministic task_output preview/downloads,
not from assistant prose. These tests encode the exact staging mismatch from
2026-05-14 so persisted assistant content cannot preserve hallucinated prices.
"""

from __future__ import annotations

import inspect


def _pricing_task_output(preview: list[dict]) -> dict:
    return {
        "type": "task_output",
        "data": {
            "type": "task_output",
            "task_kind": "pricing",
            "sku_count": 1,
            "currency_count": len(preview[0]) - 2,
            "output_files": {"excel": "file-1", "netsuite_csv": "file-2"},
            "preview": preview,
            "template_mode": False,
        },
    }


def test_pricing_task_output_suppresses_hallucinated_convert_prices():
    """Persisted prose must not keep SEK/NOK numbers that disagree with preview."""
    from app.services.chat.orchestrator import _coerce_assistant_content

    assistant_prose = (
        "Converted USD 99 across currencies: CAD 139, EUR 119, GBP 99, "
        "SEK 1339, AUD 169, NOK 1089."
    )
    persisted_output = _pricing_task_output(
        [
            {
                "SKU": "ITEM-001",
                "Item Name": None,
                "CAD": 139,
                "EUR": 119,
                "GBP": 99,
                "SEK": 1369,
                "AUD": 169,
                "NOK": 1429,
            }
        ]
    )

    content = _coerce_assistant_content(assistant_prose, persisted_output)

    assert "1339" not in content
    assert "1089" not in content
    assert "1369" not in content
    assert "1429" not in content
    assert content


def test_pricing_task_output_suppresses_hallucinated_revise_price():
    """Persisted revise prose must not claim EUR 129 when preview has EUR 159."""
    from app.services.chat.orchestrator import _coerce_assistant_content

    assistant_prose = "I've updated the EUR price to EUR 129 and recalculated the EUR-based currencies."
    persisted_output = _pricing_task_output(
        [
            {
                "SKU": "ITEM-001",
                "Item Name": None,
                "USD": 99,
                "EUR": 159,
                "SEK": 1369,
                "NOK": 1429,
            }
        ]
    )

    content = _coerce_assistant_content(assistant_prose, persisted_output)

    assert "129" not in content
    assert "159" not in content
    assert content


def test_run_chat_turn_assistant_persistence_sites_use_content_coercion():
    """Both assistant ChatMessage save sites must route through the helper.

    The unified and legacy paths can diverge, so check the exact wiring pattern
    that protects persisted assistant content from leaking card-adjacent prose.
    """
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator.run_chat_turn)

    assert source.count("_coerce_assistant_content(final_text, _persisted_output)") == 1
    assert source.count("_coerce_assistant_content(final_text, last_structured_output)") == 1


def test_run_chat_turn_streaming_paths_suppress_pricing_text_after_task_output():
    """Live SSE must stop yielding text after pricing task_output is emitted."""
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator.run_chat_turn)

    assert source.count("suppress_streamed_text = False") == 2
    assert source.count("if _is_pricing_task_output(last_structured_output):") == 2
    assert source.count("suppress_streamed_text = True") == 2
    assert "if suppress_streamed_text:\n                                continue" in source
    assert "if not suppress_streamed_text:\n                        yield {\"type\": \"text\", \"content\": payload}" in source
