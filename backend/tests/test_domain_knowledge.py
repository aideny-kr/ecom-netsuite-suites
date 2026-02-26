"""Tests for domain knowledge vector store — chunking, retrieval, agent injection."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.ingest_domain_knowledge import (
    chunk_markdown,
    estimate_tokens,
    extract_h1,
    parse_frontmatter,
)

SAMPLE_MD = """\
---
topic_tags: ["suiteql", "joins"]
source_type: expert_rules
---

# Join Patterns

## Header vs Line Aggregation

Never use SUM(t.foreigntotal) with transactionline joins.

```sql
SELECT COUNT(*) as order_count, SUM(t.foreigntotal) as total
FROM transaction t
WHERE t.type = 'SalesOrd'
```

## Line-Level Breakdown

Use SUM(tl.foreignamount) for line-level totals.

```sql
SELECT BUILTIN.DF(i.displayname) as item,
       SUM(tl.foreignamount) * -1 as revenue
FROM transactionline tl
  JOIN transaction t ON tl.transaction = t.id
GROUP BY BUILTIN.DF(i.displayname)
```
"""


# ── Frontmatter parsing ──


class TestFrontmatterParsing:
    def test_parses_topic_tags(self):
        fm, body = parse_frontmatter(SAMPLE_MD)
        assert fm["topic_tags"] == ["suiteql", "joins"]
        assert fm["source_type"] == "expert_rules"

    def test_body_excludes_frontmatter(self):
        fm, body = parse_frontmatter(SAMPLE_MD)
        assert "---" not in body.split("\n")[0]
        assert "# Join Patterns" in body

    def test_no_frontmatter(self):
        fm, body = parse_frontmatter("# Just a Title\n\nSome content.")
        assert fm == {}
        assert "# Just a Title" in body

    def test_invalid_yaml_graceful(self):
        content = "---\n: invalid: yaml: [[\n---\n\n# Title\nContent"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert "Content" in body


# ── H1 extraction ──


class TestH1Extraction:
    def test_extracts_h1(self):
        assert extract_h1("# My Title\n\nContent") == "My Title"

    def test_no_h1(self):
        assert extract_h1("## Only H2\n\nContent") is None

    def test_h1_with_extra_spaces(self):
        assert extract_h1("#   Spaced Title  \n\nContent") == "Spaced Title"


# ── Chunking ──


class TestChunking:
    def test_chunks_by_h2_headers(self):
        chunks = chunk_markdown(SAMPLE_MD, "golden_dataset/test.md")
        # Should produce at least 2 chunks (one per H2 section)
        assert len(chunks) >= 2
        # Each chunk should have the required fields
        for c in chunks:
            assert "source_uri" in c
            assert "chunk_index" in c
            assert "raw_text" in c
            assert "token_count" in c
            assert "topic_tags" in c

    def test_code_block_preserved_with_text(self):
        chunks = chunk_markdown(SAMPLE_MD, "golden_dataset/test.md")
        # Find chunk with SQL code block
        sql_chunks = [c for c in chunks if "```sql" in c["raw_text"]]
        assert len(sql_chunks) >= 1
        # SQL block should have preceding context
        for sc in sql_chunks:
            assert "```sql" in sc["raw_text"]
            # Should have text before the code block
            code_start = sc["raw_text"].index("```sql")
            text_before = sc["raw_text"][:code_start].strip()
            assert len(text_before) > 0

    def test_h1_prepended_to_chunks(self):
        chunks = chunk_markdown(SAMPLE_MD, "golden_dataset/test.md")
        for c in chunks:
            assert c["raw_text"].startswith("# Join Patterns")

    def test_topic_tags_propagated(self):
        chunks = chunk_markdown(SAMPLE_MD, "golden_dataset/test.md")
        for c in chunks:
            assert c["topic_tags"] == ["suiteql", "joins"]

    def test_source_type_propagated(self):
        chunks = chunk_markdown(SAMPLE_MD, "golden_dataset/test.md")
        for c in chunks:
            assert c["source_type"] == "expert_rules"

    def test_chunk_indices_sequential(self):
        chunks = chunk_markdown(SAMPLE_MD, "golden_dataset/test.md")
        indices = [c["chunk_index"] for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_large_section_split(self):
        """Sections > 600 tokens should be split at paragraph boundaries."""
        large_content = "---\ntopic_tags: []\nsource_type: expert_rules\n---\n\n# Big Doc\n\n## Huge Section\n\n"
        # Create a section with many paragraphs
        for i in range(30):
            large_content += f"Paragraph {i} with some content that adds tokens. " * 5 + "\n\n"
        chunks = chunk_markdown(large_content, "golden_dataset/big.md")
        # Should produce more than 1 chunk
        assert len(chunks) > 1
        for c in chunks:
            assert c["token_count"] < 700  # Should stay under ceiling

    def test_empty_content(self):
        chunks = chunk_markdown("", "golden_dataset/empty.md")
        assert chunks == []


# ── Token estimation ──


class TestTokenEstimate:
    def test_estimate(self):
        assert estimate_tokens("hello world") == 2  # 11 chars // 4
        assert estimate_tokens("a" * 400) == 100


# ── Agent prompt injection ──


class TestAgentDomainKnowledgeInjection:
    def test_domain_knowledge_injected_when_present(self):
        from app.services.chat.agents.suiteql_agent import SuiteQLAgent

        agent = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        agent._domain_knowledge = [
            "Use FETCH FIRST N ROWS ONLY for pagination.",
            "Never use LIMIT in SuiteQL.",
        ]
        prompt = agent.system_prompt
        assert "<domain_knowledge>" in prompt
        assert "</domain_knowledge>" in prompt
        assert "--- Reference 1 ---" in prompt
        assert "--- Reference 2 ---" in prompt
        assert "FETCH FIRST N ROWS ONLY" in prompt

    def test_no_domain_knowledge_when_empty(self):
        from app.services.chat.agents.suiteql_agent import SuiteQLAgent

        agent = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        agent._domain_knowledge = []
        prompt = agent.system_prompt
        assert "<domain_knowledge>" not in prompt


# ── Retrieval service ──


class TestRetrievalService:
    @pytest.mark.asyncio
    async def test_keyword_fallback_when_no_embeddings(self):
        """When OpenAI key is missing, keyword search should work."""
        from app.services.chat.domain_knowledge import retrieve_domain_knowledge

        mock_db = AsyncMock()
        # Mock the execute to return rows
        mock_chunk = MagicMock()
        mock_chunk.raw_text = "Use FETCH FIRST for pagination"
        mock_chunk.source_uri = "golden_dataset/syntax.md"
        mock_chunk.topic_tags = ["suiteql"]

        mock_result = MagicMock()
        mock_result.all.return_value = [(mock_chunk, 2)]
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.chat.domain_knowledge.embed_domain_query", return_value=None):
            results = await retrieve_domain_knowledge(mock_db, "how to paginate queries", top_k=3)

        assert len(results) == 1
        assert results[0]["raw_text"] == "Use FETCH FIRST for pagination"
        assert results[0]["keyword_hits"] == 2

    @pytest.mark.asyncio
    async def test_vector_retrieval_with_embeddings(self):
        """When embeddings are available, vector search should work."""
        from app.services.chat.domain_knowledge import retrieve_domain_knowledge

        mock_db = AsyncMock()
        mock_chunk = MagicMock()
        mock_chunk.raw_text = "Header vs line aggregation rules"
        mock_chunk.source_uri = "golden_dataset/joins.md"
        mock_chunk.topic_tags = ["suiteql", "joins"]

        mock_result = MagicMock()
        mock_result.all.return_value = [(mock_chunk, 0.15)]
        mock_db.execute = AsyncMock(return_value=mock_result)

        fake_embedding = [0.1] * 1536
        with patch("app.services.chat.domain_knowledge.embed_domain_query", return_value=fake_embedding):
            results = await retrieve_domain_knowledge(mock_db, "join patterns", top_k=3)

        assert len(results) == 1
        assert results[0]["raw_text"] == "Header vs line aggregation rules"
        assert results[0]["similarity"] == 0.85  # 1.0 - 0.15

    @pytest.mark.asyncio
    async def test_graceful_failure_returns_empty(self):
        """If everything fails, return empty list — never block chat."""
        from app.services.chat.domain_knowledge import retrieve_domain_knowledge

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=Exception("DB down"))

        with patch("app.services.chat.domain_knowledge.embed_domain_query", return_value=None):
            results = await retrieve_domain_knowledge(mock_db, "test query")

        assert results == []


# ── Golden dataset validation ──


class TestGoldenDataset:
    """Validate the golden dataset files themselves."""

    def test_all_8_files_exist(self):
        golden_dir = Path(__file__).resolve().parents[1].parent / "knowledge" / "golden_dataset"
        md_files = list(golden_dir.glob("*.md"))
        assert len(md_files) == 8, (
            f"Expected 8 golden dataset files, found {len(md_files)}: {[f.name for f in md_files]}"
        )

    def test_all_files_have_frontmatter(self):
        golden_dir = Path(__file__).resolve().parents[1].parent / "knowledge" / "golden_dataset"
        for md_file in golden_dir.glob("*.md"):
            content = md_file.read_text()
            fm, _ = parse_frontmatter(content)
            assert "topic_tags" in fm, f"{md_file.name} missing topic_tags frontmatter"
            assert "source_type" in fm, f"{md_file.name} missing source_type frontmatter"

    def test_no_limit_keyword_in_sql_examples(self):
        """SuiteQL examples must use FETCH FIRST, never LIMIT."""
        golden_dir = Path(__file__).resolve().parents[1].parent / "knowledge" / "golden_dataset"
        for md_file in golden_dir.glob("*.md"):
            content = md_file.read_text()
            # Find all SQL code blocks
            import re
            sql_blocks = re.findall(r"```sql\n(.*?)```", content, re.DOTALL)
            for block in sql_blocks:
                # LIMIT should not appear as a SQL keyword (but OK in comments/text)
                lines = block.strip().split("\n")
                for line in lines:
                    line_stripped = line.strip().upper()
                    if line_stripped.startswith("--"):
                        continue
                    assert "LIMIT " not in line_stripped or "FETCH FIRST" in line_stripped, (
                        f"{md_file.name} contains LIMIT in SQL: {line}"
                    )

    def test_all_files_produce_chunks(self):
        golden_dir = Path(__file__).resolve().parents[1].parent / "knowledge" / "golden_dataset"
        total_chunks = 0
        for md_file in golden_dir.glob("*.md"):
            content = md_file.read_text()
            source_uri = f"golden_dataset/{md_file.name}"
            chunks = chunk_markdown(content, source_uri)
            assert len(chunks) > 0, f"{md_file.name} produced 0 chunks"
            total_chunks += len(chunks)
        print(f"\nTotal golden dataset chunks: {total_chunks}")
        assert total_chunks >= 20  # Expect at least 20 total chunks from 8 files


# ── Ingestion idempotency ──


class TestIngestionIdempotency:
    def test_chunks_have_unique_source_chunk_pairs(self):
        """Each chunk should have a unique (source_uri, chunk_index) pair."""
        golden_dir = Path(__file__).resolve().parents[1].parent / "knowledge" / "golden_dataset"
        seen: set[tuple[str, int]] = set()
        for md_file in golden_dir.glob("*.md"):
            content = md_file.read_text()
            source_uri = f"golden_dataset/{md_file.name}"
            chunks = chunk_markdown(content, source_uri)
            for c in chunks:
                key = (c["source_uri"], c["chunk_index"])
                assert key not in seen, f"Duplicate chunk key: {key}"
                seen.add(key)
