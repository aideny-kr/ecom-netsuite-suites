"""Tests for unified rag_search — searches both DocChunk and DomainKnowledgeChunk."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


FAKE_TENANT_ID = str(uuid.uuid4())


def _mock_doc_chunk(title: str, content: str, source_path: str) -> MagicMock:
    chunk = MagicMock()
    chunk.title = title
    chunk.content = content
    chunk.source_path = source_path
    chunk.tenant_id = uuid.UUID(FAKE_TENANT_ID)
    return chunk


def _mock_domain_chunk(raw_text: str, source_uri: str, topic_tags: list[str] | None = None) -> MagicMock:
    chunk = MagicMock()
    chunk.raw_text = raw_text
    chunk.source_uri = source_uri
    chunk.topic_tags = topic_tags or []
    chunk.is_deprecated = False
    return chunk


class TestRagSearchUnified:
    """rag_search should return results from both DocChunk and DomainKnowledgeChunk."""

    @pytest.mark.asyncio
    async def test_returns_domain_knowledge_results(self):
        """DomainKnowledgeChunk results should appear in rag_search output."""
        from app.mcp.tools.rag_search import execute

        doc_chunk = _mock_doc_chunk("Doc Title", "Some doc content about SuiteQL", "docs/suiteql.md")
        domain_chunk = _mock_domain_chunk(
            "FETCH FIRST N ROWS ONLY is the correct pagination syntax in SuiteQL. Never use LIMIT.",
            "golden_dataset/suiteql-syntax-rules.md",
            ["suiteql", "pagination"],
        )

        mock_db = AsyncMock()
        # First execute call returns DocChunk results, second returns DomainKnowledgeChunk results
        doc_result = MagicMock()
        doc_result.all.return_value = [(doc_chunk, 0.3)]  # distance 0.3 = sim 0.7

        domain_result = MagicMock()
        domain_result.all.return_value = [(domain_chunk, 0.1)]  # distance 0.1 = sim 0.9

        mock_db.execute = AsyncMock(side_effect=[doc_result, domain_result])

        with (
            patch("app.mcp.tools.rag_search.embed_query", return_value=[0.1] * 1024),
            patch("app.mcp.tools.rag_search.embed_domain_query", return_value=[0.1] * 1536),
        ):
            result = await execute(
                {"query": "SuiteQL pagination syntax"},
                {"tenant_id": FAKE_TENANT_ID, "db": mock_db},
            )

        assert result["count"] >= 2
        sources = [r["source_path"] for r in result["results"]]
        assert "golden_dataset/suiteql-syntax-rules.md" in sources
        assert "docs/suiteql.md" in sources

    @pytest.mark.asyncio
    async def test_domain_knowledge_tagged_in_results(self):
        """Domain knowledge results should be identifiable by source prefix."""
        from app.mcp.tools.rag_search import execute

        domain_chunk = _mock_domain_chunk(
            "Use TO_CHAR(date, 'YYYY-MM-DD') for date formatting",
            "golden_dataset/date-and-time-patterns.md",
            ["suiteql", "dates"],
        )

        mock_db = AsyncMock()
        doc_result = MagicMock()
        doc_result.all.return_value = []  # No doc results

        domain_result = MagicMock()
        domain_result.all.return_value = [(domain_chunk, 0.15)]

        mock_db.execute = AsyncMock(side_effect=[doc_result, domain_result])

        with (
            patch("app.mcp.tools.rag_search.embed_query", return_value=[0.1] * 1024),
            patch("app.mcp.tools.rag_search.embed_domain_query", return_value=[0.1] * 1536),
        ):
            result = await execute(
                {"query": "date formatting in SuiteQL"},
                {"tenant_id": FAKE_TENANT_ID, "db": mock_db},
            )

        assert result["count"] >= 1
        dk_results = [r for r in result["results"] if r["source_path"].startswith("golden_dataset/")]
        assert len(dk_results) >= 1
        assert "TO_CHAR" in dk_results[0]["content"]

    @pytest.mark.asyncio
    async def test_deduplication_by_source(self):
        """Same source_path from DocChunk and DomainKnowledgeChunk should not duplicate."""
        from app.mcp.tools.rag_search import execute

        doc_chunk = _mock_doc_chunk("Rules", "FETCH FIRST pagination", "golden_dataset/suiteql-syntax-rules.md")
        domain_chunk = _mock_domain_chunk(
            "FETCH FIRST N ROWS ONLY pagination",
            "golden_dataset/suiteql-syntax-rules.md",
        )

        mock_db = AsyncMock()
        doc_result = MagicMock()
        doc_result.all.return_value = [(doc_chunk, 0.2)]

        domain_result = MagicMock()
        domain_result.all.return_value = [(domain_chunk, 0.15)]

        mock_db.execute = AsyncMock(side_effect=[doc_result, domain_result])

        with (
            patch("app.mcp.tools.rag_search.embed_query", return_value=[0.1] * 1024),
            patch("app.mcp.tools.rag_search.embed_domain_query", return_value=[0.1] * 1536),
        ):
            result = await execute(
                {"query": "pagination syntax"},
                {"tenant_id": FAKE_TENANT_ID, "db": mock_db},
            )

        # Should deduplicate — same source_path
        source_paths = [r["source_path"] for r in result["results"]]
        assert source_paths.count("golden_dataset/suiteql-syntax-rules.md") == 1

    @pytest.mark.asyncio
    async def test_graceful_fallback_when_domain_embedding_fails(self):
        """If embed_domain_query fails, rag_search should still return DocChunk results."""
        from app.mcp.tools.rag_search import execute

        doc_chunk = _mock_doc_chunk("Doc", "Some content", "docs/guide.md")

        mock_db = AsyncMock()
        doc_result = MagicMock()
        doc_result.all.return_value = [(doc_chunk, 0.2)]

        mock_db.execute = AsyncMock(return_value=doc_result)

        with (
            patch("app.mcp.tools.rag_search.embed_query", return_value=[0.1] * 1024),
            patch("app.mcp.tools.rag_search.embed_domain_query", return_value=None),
        ):
            result = await execute(
                {"query": "some query"},
                {"tenant_id": FAKE_TENANT_ID, "db": mock_db},
            )

        # Should still return doc results even though domain embedding failed
        assert result["count"] >= 1
        assert not result.get("error")

    @pytest.mark.asyncio
    async def test_domain_results_keyword_boosted(self):
        """Domain knowledge results with keyword matches should rank higher."""
        from app.mcp.tools.rag_search import execute

        domain_a = _mock_domain_chunk("General pagination rules", "golden_dataset/pagination.md")
        domain_b = _mock_domain_chunk(
            "RMA status codes: Pending Approval (A), Pending Receipt (B), Received (D,E,F,G,H)",
            "golden_dataset/transaction-types-and-statuses.md",
            ["statuses", "rma"],
        )

        mock_db = AsyncMock()
        doc_result = MagicMock()
        doc_result.all.return_value = []

        # domain_a has better vector sim but no keyword match
        domain_result = MagicMock()
        domain_result.all.return_value = [(domain_a, 0.1), (domain_b, 0.2)]

        mock_db.execute = AsyncMock(side_effect=[doc_result, domain_result])

        with (
            patch("app.mcp.tools.rag_search.embed_query", return_value=[0.1] * 1024),
            patch("app.mcp.tools.rag_search.embed_domain_query", return_value=[0.1] * 1536),
        ):
            result = await execute(
                {"query": "RMA status codes"},
                {"tenant_id": FAKE_TENANT_ID, "db": mock_db},
            )

        # domain_b should rank first because "RMA" and "status" keywords match
        assert result["count"] >= 2
        assert "RMA" in result["results"][0]["content"]
