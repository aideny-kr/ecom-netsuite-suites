import pytest
from pydantic import ValidationError

from app.schemas.report import ComposeRequest, parse_sections


def test_parse_valid_sections():
    req = ComposeRequest(
        title="Q2",
        sections=[
            {"type": "heading", "level": 1, "text": "Q2 Review"},
            {"type": "narrative", "markdown": "Revenue grew {{result:r1.total}}."},
            {"type": "metric_headline", "result_id": "m1", "label": "Revenue"},
            {"type": "chart", "result_id": "r1", "chart_type": "bar"},
            {"type": "table", "result_id": "r1"},
            {"type": "divider"},
        ],
    )
    secs = parse_sections(req.sections)
    assert [s.type for s in secs] == ["heading", "narrative", "metric_headline", "chart", "table", "divider"]


def test_reject_unknown_section_type():
    with pytest.raises(ValidationError):
        ComposeRequest(title="x", sections=[{"type": "bogus"}])
