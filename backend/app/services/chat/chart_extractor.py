"""Extract <chart> blocks from agent response text.

Charts are emitted by the BI agent as XML-wrapped JSON in response text.
This module extracts them, parses the JSON, validates against ChartData,
and returns both the cleaned text and the parsed charts.

Fault-tolerant: malformed JSON is silently skipped.
"""

from __future__ import annotations

import json
import logging
import re

from app.schemas.chart import ChartData

logger = logging.getLogger(__name__)

_CHART_RE = re.compile(r"<chart>(.*?)</chart>", re.DOTALL)

_VALID_CHART_TYPES = {"bar", "line", "pie", "area", "scatter", "donut", "histogram"}


def extract_charts(text: str) -> tuple[str, list[ChartData]]:
    """Extract <chart>JSON</chart> blocks from text.

    Returns:
        (cleaned_text, list_of_charts) — cleaned_text has all <chart> blocks removed.
        Malformed JSON blocks are silently skipped (logged, not crashed).
        Invalid chart_type defaults to "bar".
    """
    charts: list[ChartData] = []

    def _process_match(match: re.Match) -> str:
        raw_json = match.group(1).strip()
        try:
            data = json.loads(raw_json)
            # Default invalid chart_type to bar
            if data.get("chart_type") not in _VALID_CHART_TYPES:
                data["chart_type"] = "bar"
            chart = ChartData(**data)
            charts.append(chart)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("chart_extractor.parse_failed: %s", str(exc)[:100])
        return ""  # Remove the <chart> block from text

    cleaned = _CHART_RE.sub(_process_match, text)
    return cleaned, charts
