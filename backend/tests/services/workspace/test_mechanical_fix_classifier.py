"""Tests for the deny-by-default mechanical-fix classifier.

Codex finding #10: ONLY rules in the explicit allowlist are auto-fixable.
Everything else (OWASP severity, architectural concerns, governance hot-spots)
is narrate-only.
"""

from __future__ import annotations


def test_unknown_code_returns_none() -> None:
    """Unknown / made-up codes must be narrate-only (deny-by-default)."""
    from app.services.workspace.mechanical_fix_classifier import classify

    result = classify(
        code="MADE-UP-RULE",
        message="something something",
        file_path="src/FileCabinet/SuiteScripts/foo.js",
        line=42,
    )
    assert result is None


def test_owasp_a03_is_narrate_only() -> None:
    """OWASP A03 severity is judgment, not deterministic — must NOT auto-fix."""
    from app.services.workspace.mechanical_fix_classifier import classify

    result = classify(
        code="OWASP-A03",
        message="Possible injection sink in user-supplied param",
        file_path="src/FileCabinet/SuiteScripts/foo.js",
        line=12,
    )
    assert result is None


def test_deprecated_2x_api_is_fixable() -> None:
    """SUITESCRIPT-DEPRECATED-2X is in the allowlist — Oracle migration table is deterministic."""
    from app.services.workspace.mechanical_fix_classifier import (
        MechanicalFix,
        classify,
    )

    result = classify(
        code="SUITESCRIPT-DEPRECATED-2X",
        message="nlapiSearchRecord is deprecated; use N/search",
        file_path="src/FileCabinet/SuiteScripts/foo.js",
        line=7,
    )
    assert isinstance(result, MechanicalFix)
    assert result.rule_id == "netsuite-suitescript-upgrade/nlapi-to-n-search"
    assert result.replacement_summary  # non-empty


def test_governance_check_is_narrate_only() -> None:
    """Where to put a remainingUsage check is context-dependent — never auto-fix."""
    from app.services.workspace.mechanical_fix_classifier import classify

    result = classify(
        code="GOVERNANCE-CHECK",
        message="Loop without governance check on remainingUsage",
        file_path="src/FileCabinet/SuiteScripts/foo.js",
        line=88,
    )
    assert result is None


def test_allowlist_entries_are_typed() -> None:
    """Every entry in _ALLOWED_RULES MUST be a MechanicalFix instance with
    non-empty rule_id and non-empty replacement_summary. Prevents typos /
    half-finished rule additions from sneaking auto-fixes through.
    """
    from app.services.workspace.mechanical_fix_classifier import (
        _ALLOWED_RULES,
        MechanicalFix,
    )

    for code, fix in _ALLOWED_RULES.items():
        assert isinstance(fix, MechanicalFix), f"{code} is not a MechanicalFix"
        assert fix.rule_id, f"{code} has empty rule_id"
        assert fix.replacement_summary, f"{code} has empty replacement_summary"
