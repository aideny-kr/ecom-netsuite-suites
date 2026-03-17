"""Celery task for daily knowledge crawl."""

import asyncio

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.knowledge_crawler",
    queue="sync",
    soft_time_limit=600,
    time_limit=720,
)
def knowledge_crawler_task(self, source_name: str | None = None):
    """Crawl knowledge sources, chunk, embed, and store."""
    from app.core.database import async_session_factory
    from app.services.knowledge.crawler_service import crawl_source
    from app.services.knowledge.source_registry import SOURCES

    sources = SOURCES
    if source_name:
        sources = [s for s in SOURCES if s.name == source_name]
        if not sources:
            return {"error": f"Unknown source: {source_name}"}

    loop = asyncio.new_event_loop()
    try:
        results = []
        for source in sources:
            async def _crawl(src=source):
                async with async_session_factory() as db:
                    return await crawl_source(src, db)

            result = loop.run_until_complete(_crawl())
            results.append({
                "source": result.source_name,
                "pages_crawled": result.pages_crawled,
                "chunks_created": result.chunks_created,
                "chunks_updated": result.chunks_updated,
                "errors": result.errors[:5],
            })

        total_pages = sum(r["pages_crawled"] for r in results)
        total_chunks = sum(r["chunks_created"] + r["chunks_updated"] for r in results)

        return {
            "sources": results,
            "total_pages": total_pages,
            "total_chunks": total_chunks,
            "message": f"Crawled {total_pages} pages, created/updated {total_chunks} chunks",
        }
    finally:
        loop.close()
