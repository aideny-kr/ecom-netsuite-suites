"""One-shot script to sanitize corrupted UTF-8 in doc_chunks.content.

Usage:
    cd backend && python -m scripts.sanitize_doc_chunks

Finds rows where content contains invalid byte sequences that crash
PostgreSQL ILIKE queries (CharacterNotInRepertoireError) and replaces
the bad bytes with clean UTF-8.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.text import sanitize_utf8
from app.models.chat import DocChunk


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # PostgreSQL can detect bad encoding via convert_from — but simpler to
        # just pull content, sanitize in Python, and update if changed.
        # Process in batches to avoid loading all content into memory.
        batch_size = 500
        offset = 0
        total_fixed = 0

        while True:
            result = await db.execute(
                select(DocChunk.id, DocChunk.content).order_by(DocChunk.id).offset(offset).limit(batch_size)
            )
            rows = result.all()
            if not rows:
                break

            for chunk_id, content in rows:
                cleaned = sanitize_utf8(content)
                if cleaned != content:
                    await db.execute(update(DocChunk).where(DocChunk.id == chunk_id).values(content=cleaned))
                    total_fixed += 1

            offset += batch_size
            print(f"  Processed {offset} rows, fixed {total_fixed} so far...")

        await db.commit()
        print(f"\nDone. Sanitized {total_fixed} doc_chunks with corrupted UTF-8.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
