"""Tests for oracle_skill_seeder.chunk_markdown."""

import pytest

from app.services.oracle_skill_seeder import chunk_markdown, _estimate_tokens


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

    def test_hard_split_preserves_fences_on_single_giant_fenced_para(self):
        fenced = "```python\n" + "x" * 10000 + "\n```"
        md = f"## Section\n\n{fenced}\n"
        chunks = chunk_markdown(md, max_tokens=1500)
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0, f"unbalanced fence in: {chunk[:100]}"


import tempfile
from pathlib import Path

from app.services.oracle_skill_seeder import walk_oracle_skills, SLUG_MAP


class TestWalkOracleSkills:
    def test_skips_non_markdown(self, tmp_path: Path):
        skill_dir = tmp_path / ".claude" / "skills" / "netsuite-owasp-secure-coding"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("## A\n\nbody")
        (skill_dir / "records.json").write_text('{"x": 1}')
        (skill_dir / "core.d.ts").write_text("declare const x: number;")

        results = list(walk_oracle_skills(root=tmp_path))
        assert len(results) == 1
        slug, path, content = results[0]
        assert slug == "oracle/owasp"
        assert "body" in content

    def test_walks_all_seven_skills(self, tmp_path: Path):
        for skill_name in SLUG_MAP:
            d = tmp_path / ".claude" / "skills" / skill_name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"## {skill_name}\n\ncontent")

        results = list(walk_oracle_skills(root=tmp_path))
        slugs = {r[0] for r in results}
        assert slugs == set(SLUG_MAP.values())

    def test_handles_missing_skill_dir(self, tmp_path: Path):
        d = tmp_path / ".claude" / "skills" / "netsuite-owasp-secure-coding"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("## A\n\nbody")

        results = list(walk_oracle_skills(root=tmp_path))
        assert len(results) == 1

    def test_empty_skills_dir_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="run scripts/refresh-oracle-skills.sh"):
            list(walk_oracle_skills(root=tmp_path))

    def test_nested_references_md_files_included(self, tmp_path: Path):
        skill_dir = tmp_path / ".claude" / "skills" / "netsuite-owasp-secure-coding"
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("## main")
        (skill_dir / "references" / "01-injection.md").write_text("## inj\n\ndetails")

        results = list(walk_oracle_skills(root=tmp_path))
        assert len(results) == 2
        for slug, _path, _content in results:
            assert slug == "oracle/owasp"
