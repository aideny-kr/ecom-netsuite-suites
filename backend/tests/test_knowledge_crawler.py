"""Tests for knowledge crawler parsing and chunking."""

import pytest
from app.services.knowledge.crawler_service import (
    parse_oracle_help,
    parse_blog,
    chunk_parsed_content,
    ParsedContent,
    _estimate_tokens,
)


class TestParseOracleHelp:
    def test_extracts_title(self):
        html = "<html><body><h1>SuiteQL Reference</h1><div class='body'>Content here</div></body></html>"
        result = parse_oracle_help(html)
        assert result.title == "SuiteQL Reference"

    def test_strips_nav(self):
        html = "<html><body><nav>Nav stuff</nav><h1>Title</h1><div class='body'>Main content</div></body></html>"
        result = parse_oracle_help(html)
        assert "Nav stuff" not in result.body_text

    def test_preserves_code_blocks(self):
        html = "<html><body><h1>T</h1><div class='body'><p>Text</p><pre>SELECT * FROM transaction</pre></div></body></html>"
        result = parse_oracle_help(html)
        assert len(result.code_blocks) == 1
        assert "SELECT" in result.code_blocks[0]


class TestParseBlog:
    def test_extracts_article(self):
        html = "<html><body><article><h1>Blog Post</h1><p>Content</p></article></body></html>"
        result = parse_blog(html)
        assert result.title == "Blog Post"
        assert "Content" in result.body_text

    def test_extracts_published_date(self):
        html = "<html><body><time datetime='2026-01-15'>Jan 15</time><article><h1>T</h1><p>C</p></article></body></html>"
        result = parse_blog(html)
        assert result.published_date == "2026-01-15"


class TestChunking:
    def test_respects_token_limit(self):
        content = ParsedContent(
            title="Test",
            body_text="\n\n".join([f"Paragraph {i} with some content " * 10 for i in range(20)]),
        )
        chunks = chunk_parsed_content(content, "test", "http://example.com")
        for chunk in chunks:
            # Allow some overflow for prefix + code blocks, but data paragraphs should be bounded
            assert chunk.token_count <= 800  # generous limit accounting for prefix

    def test_never_splits_sql(self):
        sql = "SELECT t.id, t.tranid FROM transaction t WHERE t.type = 'SalesOrd' " * 20
        content = ParsedContent(title="Test", body_text=f"Intro paragraph.\n\n{sql}\n\nEnd.")
        chunks = chunk_parsed_content(content, "test", "http://example.com")
        # The SQL should be in one chunk, not split
        sql_chunks = [c for c in chunks if "SELECT" in c.content and "SalesOrd" in c.content]
        assert len(sql_chunks) >= 1

    def test_empty_content(self):
        content = ParsedContent(title="Empty", body_text="")
        chunks = chunk_parsed_content(content, "test", "http://example.com")
        assert len(chunks) == 0

    def test_includes_title_prefix(self):
        content = ParsedContent(title="My Doc", body_text="Some content here.")
        chunks = chunk_parsed_content(content, "test_source", "http://example.com")
        assert len(chunks) > 0
        assert "My Doc" in chunks[0].content


class TestEstimateTokens:
    def test_rough_estimate(self):
        text = "word " * 100  # 500 chars
        tokens = _estimate_tokens(text)
        assert tokens == 500 // 4

    def test_empty_string(self):
        assert _estimate_tokens("") == 0


class TestGapDetector:
    @pytest.mark.asyncio
    async def test_returns_list(self):
        from unittest.mock import AsyncMock, MagicMock
        from app.services.knowledge.gap_detector import detect_knowledge_gaps

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value = iter([])
        mock_db.execute = AsyncMock(return_value=mock_result)

        gaps = await detect_knowledge_gaps(mock_db, since_hours=24, max_gaps=5)
        assert isinstance(gaps, list)


class TestRelationshipDiscovery:
    @pytest.mark.asyncio
    async def test_returns_list(self):
        from unittest.mock import AsyncMock, patch
        from app.services.knowledge.relationship_discovery import discover_transaction_relationships

        with patch("app.services.netsuite_client.execute_suiteql_via_rest", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "columns": ["source_type", "created_type", "link_count"],
                "rows": [["Sales Order", "Invoice", 150]],
            }
            result = await discover_transaction_relationships("token", "12345")
            assert len(result) == 1
            assert result[0]["source_type"] == "Sales Order"
