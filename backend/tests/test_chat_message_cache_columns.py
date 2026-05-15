"""ChatMessage must expose first-class cache observability columns.

Before this change, cache_creation_tokens / cache_read_tokens were buried
in ``audit_events.payload`` JSON — graphable only via ad-hoc SQL. Promoting
them to columns makes cache hit rate a first-class queryable metric and is
a prerequisite for any cache SLO or dashboard.

Source: codex review of the May 2026 prompt-cache audit.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.models.chat import ChatMessage


class TestChatMessageHasCacheColumns:
    def test_cache_creation_tokens_column_exists(self):
        cols = {c.name for c in inspect(ChatMessage).columns}
        assert "cache_creation_tokens" in cols, (
            "ChatMessage must expose cache_creation_tokens as a column so cache "
            "hit rate is queryable without parsing audit_events JSON."
        )

    def test_cache_read_tokens_column_exists(self):
        cols = {c.name for c in inspect(ChatMessage).columns}
        assert "cache_read_tokens" in cols, "ChatMessage must expose cache_read_tokens as a column."

    def test_cache_columns_are_nullable_integers(self):
        mapper = inspect(ChatMessage)
        for name in ("cache_creation_tokens", "cache_read_tokens"):
            col = mapper.columns[name]
            assert col.nullable is True, f"{name} must be nullable (historic rows lack the data)"
            # Integer-family type — accept any of Integer/BigInteger/SmallInteger.
            assert "INT" in col.type.__class__.__name__.upper() or "INTEGER" in str(col.type).upper(), (
                f"{name} must be an integer column, got {col.type!r}"
            )
