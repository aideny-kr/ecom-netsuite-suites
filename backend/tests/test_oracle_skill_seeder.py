"""Tests for oracle_skill_seeder.chunk_markdown."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.sql.expression import Delete

from app.services.oracle_skill_seeder import (
    SLUG_MAP,
    _estimate_tokens,
    chunk_markdown,
    seed_all_oracle_skills,
    walk_oracle_skills,
)


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
        md = "## Code Section\n\n" + "x" * 5000 + "\n\n```javascript\nrun = () => 'hello';\n```\n\nafter fence\n"
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

    def test_yields_synthetic_markdown_from_records_json(self, tmp_path: Path):
        """records.json is converted to one synthetic markdown chunk per NetSuite record."""
        skill_dir = tmp_path / ".claude" / "skills" / "netsuite-suitescript-records-reference"
        refs = skill_dir / "references"
        refs.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("## main\n")
        records_data = {
            "records": {
                "salesorder": {
                    "recordName": "Sales Order",
                    "internalId": 30,
                    "fields": [
                        {"id": "entity", "type": "select", "label": "Customer", "required": True},
                        {"id": "trandate", "type": "date", "label": "Transaction Date"},
                    ],
                },
                "customer": {
                    "fields": [{"id": "companyname", "type": "text", "label": "Company"}],
                },
            }
        }
        (refs / "records.json").write_text(json.dumps(records_data))

        results = list(walk_oracle_skills(root=tmp_path))
        contents = [c for _, _, c in results]
        # Should have the SKILL.md plus one synthetic chunk per record
        assert any("## Record: salesorder" in c for c in contents)
        assert any("`entity` (select) (required): Customer" in c for c in contents)
        assert any("`trandate` (date): Transaction Date" in c for c in contents)
        assert any("## Record: customer" in c for c in contents)


def _make_minimal_skills_tree(tmp_path):
    """Create a stub for each of the 7 expected skill dirs."""
    for skill_name in SLUG_MAP:
        d = tmp_path / ".claude" / "skills" / skill_name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"## {skill_name}\n\nstub content for tests\n")


class TestSeederPersistence:
    @pytest.mark.asyncio
    async def test_writes_partition_per_skill(self, tmp_path):
        """All 7 partition slugs appear in the rows added to the DB."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        _make_minimal_skills_tree(tmp_path)
        with patch(
            "app.services.oracle_skill_seeder.embed_domain_texts",
            new=AsyncMock(return_value=[[0.0] * 1536]),
        ):
            await seed_all_oracle_skills(mock_db, root=tmp_path)
        partitions = {call.args[0].partition_id for call in mock_db.add.call_args_list}
        assert partitions == set(SLUG_MAP.values())

    @pytest.mark.asyncio
    async def test_idempotent_re_seed(self, tmp_path):
        """Two consecutive seed runs produce identical add counts (delete + re-insert)."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        _make_minimal_skills_tree(tmp_path)
        with patch(
            "app.services.oracle_skill_seeder.embed_domain_texts",
            new=AsyncMock(return_value=[[0.0] * 1536]),
        ):
            count1 = await seed_all_oracle_skills(mock_db, root=tmp_path)
            count2 = await seed_all_oracle_skills(mock_db, root=tmp_path)
        assert count1 == count2 > 0

    @pytest.mark.asyncio
    async def test_uses_in_clause_not_like_for_delete(self, tmp_path):
        """The DELETE statement enumerates partition IDs (IN), not LIKE prefix."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        _make_minimal_skills_tree(tmp_path)
        with patch(
            "app.services.oracle_skill_seeder.embed_domain_texts",
            new=AsyncMock(return_value=[[0.0] * 1536]),
        ):
            await seed_all_oracle_skills(mock_db, root=tmp_path)
        delete_calls = [c for c in mock_db.execute.call_args_list if isinstance(c.args[0], Delete)]
        assert len(delete_calls) >= 1
        delete_stmt = delete_calls[0].args[0]
        # Render the SQL and assert structure: must contain IN clause, not LIKE
        compiled = str(delete_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert " IN (" in compiled.upper(), f"expected IN clause, got: {compiled}"
        assert " LIKE " not in compiled.upper(), f"unexpected LIKE clause: {compiled}"

    @pytest.mark.asyncio
    async def test_writes_rows_when_embedding_unavailable(self, tmp_path):
        """When embed_domain_texts returns None, rows are still written with embedding=None."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        _make_minimal_skills_tree(tmp_path)
        with patch(
            "app.services.oracle_skill_seeder.embed_domain_texts",
            new=AsyncMock(return_value=None),
        ):
            count = await seed_all_oracle_skills(mock_db, root=tmp_path)
        assert count > 0
        # Every added chunk has embedding=None when the embedder is unavailable
        for call in mock_db.add.call_args_list:
            chunk = call.args[0]
            assert chunk.embedding is None

    @pytest.mark.asyncio
    async def test_aborts_when_no_chunks_collected(self, tmp_path):
        """If all skill files are empty, the seeder logs and returns 0 without deleting."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        # Create skill dirs with empty SKILL.md files
        for skill_name in SLUG_MAP:
            d = tmp_path / ".claude" / "skills" / skill_name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("")  # empty content → 0 chunks

        with patch(
            "app.services.oracle_skill_seeder.embed_domain_texts",
            new=AsyncMock(return_value=[[0.0] * 1536]),
        ):
            count = await seed_all_oracle_skills(mock_db, root=tmp_path)
        assert count == 0
        # Crucially: no delete was executed
        mock_db.execute.assert_not_called()
        mock_db.add.assert_not_called()

    def test_partition_id_under_64_chars(self):
        """All partition slugs fit DomainKnowledgeChunk.partition_id String(64) limit."""
        for slug in SLUG_MAP.values():
            assert len(slug) <= 64, f"{slug} exceeds 64-char limit"


class TestCLIEntry:
    def test_main_imports_and_is_callable(self):
        """Smoke test: module imports cleanly and main() exists as a callable."""
        from app.scripts import seed_oracle_skills as cli

        assert callable(cli.main)


class TestDefaultSkillsRoot:
    """The seeder must look at ORACLE_SKILLS_ROOT in production.

    The Dockerfile sets ORACLE_SKILLS_ROOT=/app and COPYs .claude/skills/ to
    /app/.claude/skills/. Without this env var honoured, Beat's reseed task
    fails with FileNotFoundError because parents[3] inside the container
    resolves to /, not /app — which silently skips Oracle RAG seeding on
    staging and prod.
    """

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        from app.services.oracle_skill_seeder import _default_skills_root

        monkeypatch.setenv("ORACLE_SKILLS_ROOT", str(tmp_path))
        assert _default_skills_root() == tmp_path

    def test_falls_back_to_parents3_when_env_unset(self, monkeypatch):
        from app.services.oracle_skill_seeder import _default_skills_root

        monkeypatch.delenv("ORACLE_SKILLS_ROOT", raising=False)
        # Walk up from oracle_skill_seeder.py: services/ → app/ → backend/ → repo root
        expected = Path(__file__).resolve().parents[2]  # tests/ → backend/ → repo root
        assert _default_skills_root() == expected

    @pytest.mark.asyncio
    async def test_seeder_uses_env_var_when_root_not_passed(self, tmp_path, monkeypatch):
        """seed_all_oracle_skills with no root arg should honour ORACLE_SKILLS_ROOT."""
        for skill_name in SLUG_MAP:
            d = tmp_path / ".claude" / "skills" / skill_name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"## {skill_name}\n\ncontent")
        monkeypatch.setenv("ORACLE_SKILLS_ROOT", str(tmp_path))

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        with patch(
            "app.services.oracle_skill_seeder.embed_domain_texts",
            new=AsyncMock(return_value=[[0.0] * 1536]),
        ):
            count = await seed_all_oracle_skills(mock_db)
        assert count > 0
        # Sanity: at least one chunk should have been added per skill
        partitions = {call.args[0].partition_id for call in mock_db.add.call_args_list}
        assert partitions == set(SLUG_MAP.values())
