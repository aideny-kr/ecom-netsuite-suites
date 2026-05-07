"""Tests for oracle_skill_seeder.chunk_markdown."""

import pytest

from app.services.oracle_skill_seeder import chunk_markdown


class TestChunkMarkdown:
    def test_single_short_section_emits_one_chunk(self):
        md = "## Section A\n\nShort body under cap.\n"
        chunks = chunk_markdown(md, max_tokens=1500)
        assert len(chunks) == 1
        assert "Section A" in chunks[0]
        assert "Short body under cap" in chunks[0]

    def test_two_h2_sections_emit_two_chunks(self):
        md = "## A\n\nbody a\n\n## B\n\nbody b\n"
        chunks = chunk_markdown(md, max_tokens=1500)
        assert len(chunks) == 2

    def test_h3_under_h2_starts_new_chunk(self):
        md = "## A\n\nbody a\n\n### A1\n\nbody a1\n"
        chunks = chunk_markdown(md, max_tokens=1500)
        assert len(chunks) == 2

    def test_subsplit_when_section_exceeds_cap(self):
        long_para = "x" * 4000
        md = f"## Long\n\n{long_para}\n\n{long_para}\n"
        chunks = chunk_markdown(md, max_tokens=1500)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert _estimate_tokens(chunk) <= 1500

    def test_preserves_code_fences(self):
        md = (
            "## Code Section\n\n"
            + "x" * 5000
            + "\n\n```javascript\nrun = () => 'hello';\n```\n\nafter fence\n"
        )
        chunks = chunk_markdown(md, max_tokens=1500)
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0, f"unbalanced fences in: {chunk[:200]}"

    def test_no_headers_falls_back_to_paragraph_split(self):
        md = "first para\n\nsecond para\n\nthird para\n"
        chunks = chunk_markdown(md, max_tokens=1500)
        assert len(chunks) == 1
        assert "first para" in chunks[0]


def _estimate_tokens(text: str) -> int:
    return len(text) // 4
