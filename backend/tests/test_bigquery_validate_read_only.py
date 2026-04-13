"""Tests for _validate_read_only — SQL comment stripping and read-only enforcement."""

import pytest

from app.services.bigquery_service import _validate_read_only


class TestValidateReadOnly:
    """Tests that _validate_read_only correctly allows SELECT/WITH and rejects everything else."""

    def test_plain_select(self):
        """Plain SELECT passes."""
        _validate_read_only("SELECT * FROM dataset.table")

    def test_select_with_leading_whitespace(self):
        """SELECT with leading whitespace passes."""
        _validate_read_only("   SELECT 1")

    def test_with_cte(self):
        """WITH/CTE query passes."""
        _validate_read_only("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_single_line_comment_before_select(self):
        """Single-line comment before SELECT should pass after fix."""
        _validate_read_only("-- comment\nSELECT * FROM dataset.table")

    def test_block_comment_before_select(self):
        """Block comment before SELECT should pass after fix."""
        _validate_read_only("/* block */ SELECT * FROM dataset.table")

    def test_multiline_block_comment_before_select(self):
        """Multi-line block comment before SELECT should pass after fix."""
        _validate_read_only("/* multi\nline */ SELECT * FROM dataset.table")

    def test_multiple_single_line_comments_before_select(self):
        """Multiple single-line comments before SELECT should pass after fix."""
        _validate_read_only("-- comment\n-- another\nSELECT * FROM dataset.table")

    def test_mixed_comments_before_select(self):
        """Mixed comment styles before SELECT should pass after fix."""
        _validate_read_only("-- first\n/* block */ SELECT * FROM dataset.table")

    def test_comment_before_with_cte(self):
        """Comment before WITH/CTE should pass after fix."""
        _validate_read_only("-- setup\nWITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_insert_rejected(self):
        """INSERT is rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("INSERT INTO dataset.table VALUES (1)")

    def test_drop_rejected(self):
        """DROP TABLE is rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("DROP TABLE dataset.table")

    def test_explain_rejected(self):
        """EXPLAIN is rejected — not read-only in BigQuery context."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("EXPLAIN SELECT 1")

    def test_empty_string_rejected(self):
        """Empty string is rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("")

    def test_only_comments_rejected(self):
        """Query with only comments and no actual SQL is rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("-- just a comment\n/* another */")

    def test_comment_with_insert_rejected(self):
        """Comment before INSERT is still rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("-- sneaky\nINSERT INTO dataset.table VALUES (1)")

    def test_delete_rejected(self):
        """DELETE is rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("DELETE FROM dataset.table WHERE id = 1")

    def test_update_rejected(self):
        """UPDATE is rejected."""
        with pytest.raises(ValueError, match="Read-only"):
            _validate_read_only("UPDATE dataset.table SET col = 1")
