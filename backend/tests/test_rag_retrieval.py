"""Tests for RAG retrieval improvements (Fix 6 — 10x Agent Quality).

Tests keyword boosting, H2 title prepending, and merged retrieval.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.ingest_domain_knowledge import chunk_markdown


class TestH2TitlePrepending:
    """Chunks should prepend both H1 and H2 titles for richer embedding context."""

    SAMPLE = """\
---
topic_tags: ["test"]
source_type: expert_rules
---

# Main Title

## Section Alpha

Content about alpha.

## Section Beta

Content about beta.
"""

    def test_h2_title_in_chunk(self):
        chunks = chunk_markdown(self.SAMPLE, "test.md")
        alpha_chunks = [c for c in chunks if "alpha" in c["raw_text"].lower()]
        assert len(alpha_chunks) >= 1
        # Should have "Main Title — Section Alpha" format
        assert "Main Title — Section Alpha" in alpha_chunks[0]["raw_text"]

    def test_h2_title_different_per_section(self):
        chunks = chunk_markdown(self.SAMPLE, "test.md")
        titles = [c["raw_text"].split("\n")[0] for c in chunks]
        # Each chunk should have a different H2 in its title
        assert any("Alpha" in t for t in titles)
        assert any("Beta" in t for t in titles)

    def test_rma_chunk_has_h2_in_title(self):
        """The real golden dataset RMA status chunk should have 'Return Authorization' in title."""
        golden_dir = Path(__file__).resolve().parents[1].parent / "knowledge" / "golden_dataset"
        content = (golden_dir / "transaction-types-and-statuses.md").read_text()
        chunks = chunk_markdown(content, "golden_dataset/transaction-types-and-statuses.md")
        # Find the chunk with the RMA STATUS section (not just any mention of RtnAuth)
        rma_status_chunks = [c for c in chunks if "Return Authorization (RMA) Statuses" in c["raw_text"]
                             or "Return Authorization" in c["raw_text"].split("\n")[0]]
        assert len(rma_status_chunks) >= 1
        first_line = rma_status_chunks[0]["raw_text"].split("\n")[0]
        assert "—" in first_line  # H1 — H2 format


class TestKeywordBoosting:
    """Vector results should be re-ranked by keyword overlap."""

    @pytest.mark.asyncio
    async def test_keyword_boost_reranks_results(self):
        """A chunk with keyword matches should rank higher than pure vector similarity."""
        from app.services.chat.domain_knowledge import retrieve_domain_knowledge

        # Create mock chunks: chunk_a has higher vector sim but no keyword match
        # chunk_b has lower vector sim but matches "RMA" keyword
        chunk_a = MagicMock()
        chunk_a.raw_text = "General SuiteQL syntax rules for pagination"
        chunk_a.source_uri = "golden_dataset/syntax.md"
        chunk_a.topic_tags = ["suiteql"]
        chunk_a.is_deprecated = False

        chunk_b = MagicMock()
        chunk_b.raw_text = "Return Authorization (RMA) Statuses — RtnAuth status codes for received RMAs"
        chunk_b.source_uri = "golden_dataset/statuses.md"
        chunk_b.topic_tags = ["statuses"]
        chunk_b.is_deprecated = False

        mock_db = AsyncMock()
        # chunk_a: distance 0.1 (sim 0.9), chunk_b: distance 0.2 (sim 0.8)
        mock_result = MagicMock()
        mock_result.all.return_value = [(chunk_a, 0.1), (chunk_b, 0.2)]
        mock_db.execute = AsyncMock(return_value=mock_result)

        fake_embedding = [0.1] * 1536
        with patch("app.services.chat.domain_knowledge.embed_domain_query", return_value=fake_embedding):
            results = await retrieve_domain_knowledge(mock_db, "show me RMA statuses", top_k=2)

        # chunk_b should rank first because "RMA" and "statuses" keywords match
        assert len(results) == 2
        assert "RMA" in results[0]["raw_text"]
        assert results[0]["keyword_hits"] > results[1]["keyword_hits"]

    @pytest.mark.asyncio
    async def test_adjusted_score_includes_keyword_hits(self):
        """Results should include adjusted_score field."""
        from app.services.chat.domain_knowledge import retrieve_domain_knowledge

        chunk = MagicMock()
        chunk.raw_text = "RMA received status codes D E F G H"
        chunk.source_uri = "test.md"
        chunk.topic_tags = []

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [(chunk, 0.2)]
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.chat.domain_knowledge.embed_domain_query", return_value=[0.1] * 1536):
            results = await retrieve_domain_knowledge(mock_db, "RMA received", top_k=3)

        assert len(results) == 1
        assert "adjusted_score" in results[0]
        assert results[0]["adjusted_score"] > results[0]["similarity"]
