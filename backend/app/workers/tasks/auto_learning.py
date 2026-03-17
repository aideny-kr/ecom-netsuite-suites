"""Celery task for auto-learning from knowledge gaps."""

import asyncio
import hashlib
import uuid

import structlog

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = structlog.get_logger()

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
                # Staleness check: re-run partial discovery for tenants with >30 day profiles
                await _refresh_stale_profiles(db)

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
                    except Exception as e:
                        logger.warning("auto_learning.search_failed", query=search_query, error=str(e))
                        search_results = []

                    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                        for sr in search_results[:3]:
                            url = sr.get("href") or sr.get("link", "")
                            if not url:
                                continue

                            try:
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
                                url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
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

                            except Exception as e:
                                logger.warning("auto_learning.url_failed", url=url, error=str(e))
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


async def _refresh_stale_profiles(db):
    """Check all tenants for stale onboarding profiles (>30 days) and refresh Phases 1+4."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.core.encryption import decrypt_credentials
    from app.models.connection import Connection
    from app.models.tenant import TenantConfig
    from app.services.knowledge.onboarding_discovery import (
        _discover_status_codes,
        _discover_transaction_types,
    )
    from app.services.netsuite_oauth_service import get_valid_token

    import structlog
    logger = structlog.get_logger()

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    result = await db.execute(
        select(TenantConfig)
        .where(TenantConfig.onboarding_profile.isnot(None))
        .order_by(TenantConfig.updated_at.asc())
        .limit(10)
    )
    configs = result.scalars().all()

    for tc in configs:
        profile = tc.onboarding_profile or {}
        discovered_at = profile.get("discovered_at")
        if not discovered_at:
            continue

        try:
            disc_dt = datetime.fromisoformat(discovered_at)
            if disc_dt > cutoff:
                continue  # Fresh enough
        except (ValueError, TypeError):
            continue

        # Stale — refresh Phase 1 + Phase 4
        conn_result = await db.execute(
            select(Connection).where(
                Connection.tenant_id == tc.tenant_id,
                Connection.provider == "netsuite",
                Connection.status == "active",
            ).limit(1)
        )
        connection = conn_result.scalar_one_or_none()
        if not connection:
            continue

        try:
            access_token = await get_valid_token(db, connection)
            if not access_token:
                continue

            creds = decrypt_credentials(connection.encrypted_credentials)
            account_id = (creds.get("account_id") or "").replace("_", "-").lower()

            # Phase 1: Transaction landscape
            phase1 = await _discover_transaction_types(access_token, account_id)
            if phase1.success and phase1.data:
                profile["transaction_types"] = phase1.data

            # Phase 4: Status codes for top types
            top_types = [t["type"] for t in (phase1.data or [])[:10] if t.get("count", 0) > 10]
            if top_types:
                phase4 = await _discover_status_codes(access_token, account_id, top_types)
                if phase4.success and phase4.data:
                    profile["status_codes"] = phase4.data

            profile["discovered_at"] = datetime.now(timezone.utc).isoformat()
            profile["last_refresh_reason"] = "staleness_30d"

            from sqlalchemy.orm.attributes import flag_modified
            tc.onboarding_profile = profile
            flag_modified(tc, "onboarding_profile")

            logger.info("auto_learning.stale_profile_refreshed", tenant_id=str(tc.tenant_id))
        except Exception as e:
            logger.warning("auto_learning.stale_refresh_failed", tenant_id=str(tc.tenant_id), error=str(e))
