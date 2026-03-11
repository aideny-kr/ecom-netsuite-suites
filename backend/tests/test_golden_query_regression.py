"""Golden Query Regression Suite — validates importance classification,
SQL structure, and judge threshold enforcement for 30 known-good queries.

Runs in CI without live NetSuite credentials.
"""

import re

import pytest

from app.mcp.tools.netsuite_suiteql import is_read_only_sql, parse_tables
from app.services.importance_classifier import ImportanceTier, classify_importance
from app.services.suiteql_judge import EnforcementResult, JudgeVerdict, enforce_judge_threshold
from tests.fixtures.golden_queries import load_golden_queries, validate_schema

GOLDEN_QUERIES = load_golden_queries()


# ---------------------------------------------------------------------------
# Cycle 2+3 — Schema validation + Importance tier
# ---------------------------------------------------------------------------


class TestGoldenQuerySchema:
    """Validate the fixture file itself."""

    def test_fixture_has_30_queries(self):
        assert len(GOLDEN_QUERIES) == 30

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_schema_valid(self, spec):
        validate_schema(spec)

    def test_tier_distribution(self):
        tiers = [q["tier"] for q in GOLDEN_QUERIES]
        assert tiers.count(1) >= 6, "Need at least 6 casual queries"
        assert tiers.count(2) >= 6, "Need at least 6 operational queries"
        assert tiers.count(3) >= 6, "Need at least 6 reporting queries"
        assert tiers.count(4) >= 4, "Need at least 4 audit-critical queries"


class TestGoldenQueryImportance:
    """Verify importance classifier assigns correct tier for each golden query."""

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_importance_tier(self, spec):
        tier = classify_importance(spec["question"])
        assert tier.value == spec["tier"], (
            f"Query '{spec['question']}' classified as {tier.label} (tier {tier.value}), "
            f"expected tier {spec['tier']} ({ImportanceTier(spec['tier']).label})"
        )


# ---------------------------------------------------------------------------
# Cycle 4 — SQL structure validation
# ---------------------------------------------------------------------------


class TestGoldenQuerySQL:
    """Validate sample SQL structure for each golden query."""

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_is_read_only(self, spec):
        """All golden queries must be read-only SELECT statements."""
        assert is_read_only_sql(spec["sample_sql"]), (
            f"Golden query {spec['id']} SQL is not read-only: {spec['sample_sql'][:100]}"
        )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_contains_expected(self, spec):
        """SQL should contain all expected keywords/patterns."""
        sql_upper = spec["sample_sql"].upper()
        for keyword in spec["expected_sql_contains"]:
            assert keyword.upper() in sql_upper, (
                f"Golden query {spec['id']} SQL missing '{keyword}': "
                f"{spec['sample_sql'][:200]}"
            )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_not_contains_forbidden(self, spec):
        """SQL should NOT contain forbidden keywords."""
        sql_upper = spec["sample_sql"].upper()
        for keyword in spec["expected_sql_not_contains"]:
            assert keyword.upper() not in sql_upper, (
                f"Golden query {spec['id']} SQL contains forbidden '{keyword}': "
                f"{spec['sample_sql'][:200]}"
            )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_references_expected_tables(self, spec):
        """SQL should reference the expected tables."""
        tables = parse_tables(spec["sample_sql"])
        for expected_table in spec["expected_tables"]:
            assert expected_table.lower() in tables, (
                f"Golden query {spec['id']} SQL doesn't reference table '{expected_table}'. "
                f"Found tables: {tables}"
            )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_uses_single_letter_status_codes(self, spec):
        """SQL should use single-letter status codes, not compound ones."""
        compound_pattern = re.compile(r"(?:SalesOrd|PurchOrd|CustInvc|VendBill):[A-Z]")
        match = compound_pattern.search(spec["sample_sql"])
        assert match is None, (
            f"Golden query {spec['id']} uses compound status code: {match.group()}. "
            f"Use single-letter codes instead."
        )


# ---------------------------------------------------------------------------
# Cycle 5 — Judge threshold validation
# ---------------------------------------------------------------------------


class TestGoldenQueryJudge:
    """Validate judge enforcement thresholds for each tier."""

    @pytest.mark.parametrize(
        "spec",
        [q for q in GOLDEN_QUERIES if q["tier"] >= 2],
        ids=lambda s: s["id"],
    )
    def test_tier_2_plus_has_judge_threshold(self, spec):
        """Tier 2+ queries should have meaningful judge thresholds."""
        tier = ImportanceTier(spec["tier"])
        assert tier.judge_confidence_threshold > 0, (
            f"Tier {spec['tier']} should have a non-zero judge threshold"
        )

    def test_casual_passes_with_low_confidence(self):
        """Tier 1 queries should always pass regardless of confidence."""
        verdict = JudgeVerdict(approved=True, confidence=0.2, reason="Low confidence")
        result = enforce_judge_threshold(verdict, ImportanceTier.CASUAL)
        assert isinstance(result, EnforcementResult)
        assert result.passed is True

    def test_operational_fails_below_threshold(self):
        """Tier 2 queries should fail below threshold."""
        threshold = ImportanceTier.OPERATIONAL.judge_confidence_threshold
        verdict = JudgeVerdict(approved=True, confidence=threshold - 0.1, reason="Moderate")
        result = enforce_judge_threshold(verdict, ImportanceTier.OPERATIONAL)
        assert result.passed is False

    def test_reporting_fails_below_threshold(self):
        """Tier 3 queries should fail below threshold."""
        threshold = ImportanceTier.REPORTING.judge_confidence_threshold
        verdict = JudgeVerdict(approved=True, confidence=threshold - 0.1, reason="Good but not great")
        result = enforce_judge_threshold(verdict, ImportanceTier.REPORTING)
        assert result.passed is False

    def test_audit_critical_flags_for_review(self):
        """Tier 4 queries below threshold should flag for human review."""
        threshold = ImportanceTier.AUDIT_CRITICAL.judge_confidence_threshold
        verdict = JudgeVerdict(approved=True, confidence=threshold - 0.05, reason="Pretty good")
        result = enforce_judge_threshold(verdict, ImportanceTier.AUDIT_CRITICAL)
        assert result.passed is False
        assert result.needs_review is True

    def test_audit_critical_passes_high_confidence(self):
        """Tier 4 queries with high confidence should pass."""
        threshold = ImportanceTier.AUDIT_CRITICAL.judge_confidence_threshold
        verdict = JudgeVerdict(approved=True, confidence=threshold + 0.05, reason="Excellent")
        result = enforce_judge_threshold(verdict, ImportanceTier.AUDIT_CRITICAL)
        assert result.passed is True
        assert result.needs_review is False
