"""Re-embed doc_chunks that have NULL embeddings using OpenAI.

Usage:
    cd backend && python -m scripts.reembed_doc_chunks

Processes in batches of 20 to respect rate limits.
Uses the same embed_texts() function as the RAG seeder (OpenAI primary).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.chat import DocChunk
from app.services.chat.embeddings import embed_texts


async def main() -> None:
    if not settings.OPENAI_EMBEDDING_API_KEY:
        print("ERROR: OPENAI_EMBEDDING_API_KEY not configured in .env")
        sys.exit(1)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    batch_size = 20
    total_embedded = 0

    async with async_session() as db:
        # Count total NULL-embedding rows
        count_result = await db.execute(select(DocChunk.id).where(DocChunk.embedding.is_(None)))
        null_ids = [row[0] for row in count_result.all()]
        total_null = len(null_ids)
        print(f"Found {total_null} doc_chunks with NULL embeddings.")

        if total_null == 0:
            print("Nothing to do.")
            await engine.dispose()
            return

        # Process in batches
        for i in range(0, total_null, batch_size):
            batch_ids = null_ids[i : i + batch_size]

            # Fetch content for this batch
            result = await db.execute(select(DocChunk.id, DocChunk.content).where(DocChunk.id.in_(batch_ids)))
            rows = result.all()

            if not rows:
                break

            chunk_ids = [r[0] for r in rows]
            texts = [r[1] for r in rows]

            # Embed — try batch first, fall back to individual on failure
            embeddings = await embed_texts(texts)
            if embeddings is None:
                # Batch failed (likely oversized chunk) — try one by one
                individual_count = 0
                for chunk_id, text in zip(chunk_ids, texts):
                    # Truncate to ~7000 tokens (~28000 chars) to stay under 8192 limit
                    truncated = text[:28000] if len(text) > 28000 else text
                    single = await embed_texts([truncated])
                    if single is not None:
                        await db.execute(update(DocChunk).where(DocChunk.id == chunk_id).values(embedding=single[0]))
                        individual_count += 1
                total_embedded += individual_count
                print(
                    f"  Batch {i // batch_size + 1}: batch failed, embedded {individual_count}/{len(chunk_ids)} individually ({total_embedded}/{total_null})"
                )
                await db.commit()
                if i + batch_size < total_null:
                    await asyncio.sleep(0.5)
                continue

            # Update each row
            for chunk_id, embedding in zip(chunk_ids, embeddings):
                await db.execute(update(DocChunk).where(DocChunk.id == chunk_id).values(embedding=embedding))

            total_embedded += len(embeddings)
            print(f"  Batch {i // batch_size + 1}: embedded {len(embeddings)} chunks ({total_embedded}/{total_null})")

            await db.commit()

            # Brief pause between batches to be polite to rate limits
            if i + batch_size < total_null:
                await asyncio.sleep(0.5)

        print(f"\nDone. Embedded {total_embedded} of {total_null} doc_chunks.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
