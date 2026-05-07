"""Tests for the Beat task that re-seeds Oracle skills on hash change."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.oracle_skill_seeder import SLUG_MAP


def _make_skills_tree_with_lockfile(tmp_path: Path, hash_value: str):
    """Stub all 7 skill dirs + a skills-lock.json with the given hash for each."""
    for skill_name in SLUG_MAP:
        d = tmp_path / ".claude" / "skills" / skill_name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"## {skill_name}\n\ncontent\n")

    lock = {
        "version": 1,
        "skills": {name: {"computedHash": hash_value} for name in SLUG_MAP},
    }
    (tmp_path / "skills-lock.json").write_text(json.dumps(lock))


@pytest.mark.asyncio
class TestOracleSkillReseed:
    async def test_no_op_when_hashes_match(self, tmp_path):
        """First run seeds and writes sentinels; second run with unchanged lock = no-op."""
        from app.workers.tasks.oracle_skill_reseed import _run_reseed

        _make_skills_tree_with_lockfile(tmp_path, hash_value="abc123")
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        # First run: empty stored hashes → reseed everything
        with patch(
            "app.workers.tasks.oracle_skill_reseed._read_stored_hashes",
            new=AsyncMock(return_value={}),
        ), patch(
            "app.workers.tasks.oracle_skill_reseed.embed_domain_texts",
            new=AsyncMock(return_value=None),
        ):
            count1 = await _run_reseed(mock_db, root=tmp_path)
            assert count1 > 0

        # Second run: stored hashes match lockfile → 0 reseeds
        stored = {slug: "abc123" for slug in SLUG_MAP.values()}
        with patch(
            "app.workers.tasks.oracle_skill_reseed._read_stored_hashes",
            new=AsyncMock(return_value=stored),
        ), patch(
            "app.workers.tasks.oracle_skill_reseed.embed_domain_texts",
            new=AsyncMock(return_value=None),
        ):
            count2 = await _run_reseed(mock_db, root=tmp_path)
            assert count2 == 0

    async def test_reseeds_only_changed_skill(self, tmp_path):
        """If only one skill's hash changed, only that partition reseeds."""
        from app.workers.tasks.oracle_skill_reseed import _run_reseed

        _make_skills_tree_with_lockfile(tmp_path, hash_value="abc")

        # All stored hashes match except OWASP
        stored = {slug: "abc" for slug in SLUG_MAP.values()}
        stored["oracle/owasp"] = "OLD_HASH"

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        with patch(
            "app.workers.tasks.oracle_skill_reseed._read_stored_hashes",
            new=AsyncMock(return_value=stored),
        ), patch(
            "app.workers.tasks.oracle_skill_reseed.embed_domain_texts",
            new=AsyncMock(return_value=None),
        ):
            count = await _run_reseed(mock_db, root=tmp_path)

        assert count > 0
        # Only the OWASP partition should be in the add calls
        partitions_touched = {call.args[0].partition_id for call in mock_db.add.call_args_list}
        assert partitions_touched == {"oracle/owasp"}

    async def test_returns_zero_when_lockfile_missing(self, tmp_path):
        """If skills-lock.json doesn't exist, log + return 0 without touching DB."""
        from app.workers.tasks.oracle_skill_reseed import _run_reseed

        # No lockfile created
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        count = await _run_reseed(mock_db, root=tmp_path)
        assert count == 0
        mock_db.add.assert_not_called()
