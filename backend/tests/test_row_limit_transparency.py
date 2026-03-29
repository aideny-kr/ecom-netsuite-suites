"""Tests for R4: Transparent row limits."""


from app.mcp.tools.netsuite_suiteql import enforce_limit_with_metadata


class TestEnforceLimitMetadata:
    """enforce_limit_with_metadata should return metadata about capping."""

    def test_returns_capped_flag_when_limit_reduced(self):
        """When user requests 1000 but max_rows=100, return was_capped=True."""
        result = enforce_limit_with_metadata(
            "SELECT * FROM transaction FETCH FIRST 1000 ROWS ONLY",
            max_rows=100,
        )
        assert result["query"].endswith("FETCH FIRST 100 ROWS ONLY")
        assert result["was_capped"] is True
        assert result["requested_rows"] == 1000
        assert result["actual_limit"] == 100

    def test_no_capped_flag_when_within_limit(self):
        """When user requests 50 and max_rows=100, was_capped=False."""
        result = enforce_limit_with_metadata(
            "SELECT * FROM transaction FETCH FIRST 50 ROWS ONLY",
            max_rows=100,
        )
        assert result["was_capped"] is False
        assert result["requested_rows"] == 50

    def test_no_capped_flag_when_no_user_limit(self):
        """When user didn't specify a limit, was_capped=False."""
        result = enforce_limit_with_metadata(
            "SELECT * FROM transaction",
            max_rows=100,
        )
        assert result["was_capped"] is False
        assert result["requested_rows"] is None

    def test_handles_limit_keyword(self):
        """Also detects LIMIT N (even though SuiteQL prefers FETCH FIRST)."""
        result = enforce_limit_with_metadata(
            "SELECT * FROM transaction LIMIT 500",
            max_rows=100,
        )
        assert result["was_capped"] is True
        assert result["requested_rows"] == 500
