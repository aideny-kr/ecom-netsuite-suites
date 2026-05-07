"""CLI entry point for seeding Oracle SuiteCloud SDK skill content into RAG.

Usage:
    python -m app.scripts.seed_oracle_skills
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.database import async_session_factory
from app.services.oracle_skill_seeder import seed_all_oracle_skills

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    """Seed all Oracle skills, commit, return exit code."""
    async with async_session_factory() as db:
        try:
            count = await seed_all_oracle_skills(db)
            await db.commit()
            logger.info("Seeded %d Oracle skill chunks", count)
            return 0
        except FileNotFoundError as e:
            logger.error("%s", e)
            return 1
        except Exception:
            logger.exception("Oracle seeder failed")
            await db.rollback()
            return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
