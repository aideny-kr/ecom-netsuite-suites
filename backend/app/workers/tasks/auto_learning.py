"""Celery task for auto-learning from knowledge gaps."""

import asyncio
import hashlib
import uuid

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

# System-level knowledge uses a fixed tenant ID (not tenant-scoped)
SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.auto_learning",
    queue="sync",
    soft_time_limit=300,
    time_limit=420,
)
def auto_learning_task(self):
    """Detect knowledge gaps and auto-fill them via web search."""
    from app.core.database import async_session_factory
    from app.services.knowledge.gap_detector import detect_knowledge_gaps

    loop = asyncio.new_event_loop()
    try:
        async def _run():
            async with async_session_factory() as db:
                gaps = await detect_knowledge_gaps(db, since_hours=24, max_gaps=5)

                if not gaps:
                    return {"gaps_found": 0, "message": "No knowledge gaps detected"}

                import httpx

                from app.models.chat import DocChunk
                from app.services.chat.embeddings import embed_texts
                from app.services.knowledge.crawler_service import chunk_parsed_content, parse_blog

                total_chunks = 0
                topics_filled = []

                for gap in gaps:
                    search_query = f"NetSuite SuiteQL {gap.topic.replace('_', ' ')}"
                    if gap.record_types:
                        search_query += f" {gap.record_types[0]}"

                    # Web search via DuckDuckGo
                    try:
                        from ddgs import DDGS

                        with DDGS() as ddgs:
                            search_results = list(ddgs.text(search_query, max_results=3))
                    except Exception:
                        search_results = []

                    for sr in search_results[:3]:
                        url = sr.get("href") or sr.get("link", "")
                        if not url:
                            continue

                        try:
                            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                                resp = await client.get(url)
                                if resp.status_code != 200:
                                    continue

                            parsed = parse_blog(resp.text)
                            if not parsed.body_text or len(parsed.body_text) < 100:
                                continue

                            chunks = chunk_parsed_content(parsed, "auto_learned", url)
                            if not chunks:
                                continue

                            texts = [c.content for c in chunks]
                            embeddings = await embed_texts(texts)

                            topic_slug = gap.topic[:40].replace(" ", "_")
                            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                            source_path = f"auto_learned/{topic_slug}/{url_hash}"

                            for i, chunk in enumerate(chunks):
                                embedding = embeddings[i] if embeddings and i < len(embeddings) else None
                                doc_chunk = DocChunk(
                                    tenant_id=SYSTEM_TENANT_ID,
                                    source_path=source_path,
                                    title=parsed.title,
                                    chunk_index=i,
                                    content=chunk.content,
                                    token_count=chunk.token_count,
                                    embedding=embedding if embedding else None,
                                    metadata_={
                                        **chunk.metadata,
                                        "source_type": "auto_learned",
                                        "gap_topic": gap.topic,
                                        "gap_score": gap.gap_score,
                                    },
                                )
                                db.add(doc_chunk)
                                total_chunks += 1

                        except Exception:
                            continue

                    topics_filled.append(gap.topic)

                await db.commit()

                return {
                    "gaps_found": len(gaps),
                    "topics_filled": topics_filled,
                    "chunks_created": total_chunks,
                    "message": f"Filled {len(topics_filled)} gaps with {total_chunks} chunks",
                }

        return loop.run_until_complete(_run())
    finally:
        loop.close()
