"""Validate-output parser unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.workspace.validate_parser import (
    PARSER_VERSION,
    ValidationParseResult,
    parse_suitecloud_validate_output,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parser_handles_clean_run() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_clean.txt"))
    assert result.hits == []
    assert result.has_errors is False
    assert result.has_warnings is False
    assert result.parser_version == PARSER_VERSION


def test_parser_extracts_errors() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_errors.txt"))
    assert len(result.hits) == 2
    assert result.has_errors is True
    assert result.has_warnings is False
    first = result.hits[0]
    assert first.severity == "error"
    assert first.file_path == "src/Suitelets/processOrder.js"
    assert first.line == 42
    assert first.code == "OWASP-A03"
    assert "Unsanitized" in first.message


def test_parser_extracts_warnings() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_warnings.txt"))
    assert len(result.hits) == 2
    assert result.has_errors is False
    assert result.has_warnings is True
    assert all(h.severity == "warning" for h in result.hits)


def test_parser_handles_mixed_severity() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_mixed.txt"))
    assert result.has_errors is True
    assert result.has_warnings is True
    severities = {h.severity for h in result.hits}
    assert severities == {"error", "warning"}


def test_parser_falls_back_on_malformed() -> None:
    raw = _load("suitecloud_validate_malformed.txt")
    result = parse_suitecloud_validate_output(raw)
    assert len(result.hits) == 1
    assert result.hits[0].severity == "parser_error"
    assert result.hits[0].message  # raw output is preserved in the synthetic hit
    assert result.raw_output == raw


def test_parser_handles_empty_input() -> None:
    result = parse_suitecloud_validate_output("")
    assert result.hits == []
    assert result.has_errors is False
    assert result.has_warnings is False


def test_fingerprint_is_stable_across_runs() -> None:
    raw = _load("suitecloud_validate_errors.txt")
    a = parse_suitecloud_validate_output(raw)
    b = parse_suitecloud_validate_output(raw)
    assert [h.fingerprint for h in a.hits] == [h.fingerprint for h in b.hits]
    # sha256 hexdigest must be exactly 64 chars (matches WorkspaceRun.parser_version sibling
    # field validation_hits.fingerprint String(64) from Task 1).
    for hit in a.hits:
        assert len(hit.fingerprint) == 64


def test_parser_falls_back_when_garbage_starts_with_known_prefix() -> None:
    """A line that starts with `ERROR:` but doesn't match the diagnostic regex
    must still trigger the parser_error fallback. The terminal-status gate
    (SUCCESS:/FAILURE:) is what distinguishes a clean run with no findings
    from truncated/garbage output."""
    raw = "ERROR: this goes off the rails\nblah blah\nstuff stuff"
    result = parse_suitecloud_validate_output(raw)
    assert len(result.hits) == 1
    assert result.hits[0].severity == "parser_error"


def test_fingerprints_differ_within_a_run() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_errors.txt"))
    fingerprints = {h.fingerprint for h in result.hits}
    assert len(fingerprints) == len(result.hits)


def test_parser_error_fingerprint_is_stable() -> None:
    raw = _load("suitecloud_validate_malformed.txt")
    a = parse_suitecloud_validate_output(raw)
    b = parse_suitecloud_validate_output(raw)
    assert a.hits[0].severity == b.hits[0].severity == "parser_error"
    assert a.hits[0].fingerprint == b.hits[0].fingerprint
