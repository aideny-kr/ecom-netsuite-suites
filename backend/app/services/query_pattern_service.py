"""Query pattern service — extract, store, and retrieve proven SuiteQL patterns.

After a successful SuiteQL tool call, this service:
1. Extracts the SQL, tables, and columns from the tool call log.
2. Embeds the user question via OpenAI text-embedding-3-small.
3. Upserts the pattern into tenant_query_patterns (increments success_count if exists).

For retrieval, it embeds the current question and finds similar proven patterns
via pgvector cosine similarity.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_query_pattern import TenantQueryPattern
from app.services.chat.tool_call_results import tool_call_had_error, tool_call_row_count

_logger = logging.getLogger(__name__)

# Regex to extract table names from SQL (FROM/JOIN clauses)
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+(\w+)", re.IGNORECASE)

# Regex to extract column references (alias.column or bare column in SELECT)
_COLUMN_RE = re.compile(r"\b(\w+\.\w+)\b")


def _extract_tables(sql: str) -> list[str]:
    """Extract table names from FROM/JOIN clauses."""
    return list({m.lower() for m in _TABLE_RE.findall(sql)})


def _extract_columns(sql: str) -> list[str]:
    """Extract alias.column references from SQL."""
    return list({m.lower() for m in _COLUMN_RE.findall(sql)})


async def _embed_text(text_to_embed: str) -> list[float] | None:
    """Embed text using OpenAI text-embedding-3-small (1536-dim)."""
    try:
        import openai

        from app.core.config import settings

        api_key = settings.OPENAI_EMBEDDING_API_KEY
        if not api_key:
            _logger.warning("query_pattern.no_embedding_key")
            return None

        client = openai.AsyncOpenAI(api_key=api_key)
        response = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text_to_embed,
            dimensions=1536,
        )
        return response.data[0].embedding
    except Exception:
        _logger.warning("query_pattern.embedding_failed", exc_info=True)
        return None


def _is_analytical_query(sql: str) -> bool:
    """Check if a query is analytical (GROUP BY, aggregation) vs a simple probe."""
    sql_upper = sql.upper()
    has_aggregation = any(fn in sql_upper for fn in ("GROUP BY", "SUM(", "COUNT(", "AVG(", "MIN(", "MAX("))
    has_reasonable_limit = "FETCH FIRST 5 ROWS" not in sql_upper
    # Reject simple probes like SELECT * FROM item WHERE id IN (...)
    is_probe = "ROWNUM" in sql_upper and "SELECT *" in sql_upper.replace("  ", " ")
    return has_aggregation and has_reasonable_limit and not is_probe


async def extract_and_store_pattern(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_question: str,
    tool_calls_log: list[dict],
) -> bool:
    """Extract successful SuiteQL queries from tool call log and store as patterns.

    Only stores analytical queries (GROUP BY / aggregation) that returned data.
    Skips exploratory probes and failed queries.

    Returns True if at least one pattern was stored.
    """
    stored = False

    for call in tool_calls_log:
        if call.get("tool") != "netsuite_suiteql":
            continue

        params = call.get("params", {})
        query = params.get("query", "")
        if not query:
            continue

        # Check if the result was successful (not an error) and returned rows
        if tool_call_had_error(call):
            continue
        if tool_call_row_count(call) == 0:
            continue

        # Only store analytical queries, not exploratory probes
        if not _is_analytical_query(query):
            _logger.debug("query_pattern.skip_non_analytical", sql_preview=query[:100])
            continue

        tables = _extract_tables(query)
        columns = _extract_columns(query)

        # Embed the user question for semantic retrieval
        embedding = await _embed_text(user_question)

        # Upsert: insert or increment success_count
        stmt = pg_insert(TenantQueryPattern).values(
            tenant_id=tenant_id,
            user_question=user_question,
            working_sql=query,
            tables_used=tables,
            columns_used=columns,
            intent_embedding=embedding,
            success_count=1,
            last_used_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_tenant_query_pattern",
            set_={
                "success_count": TenantQueryPattern.success_count + 1,
                "last_used_at": datetime.now(timezone.utc),
                "intent_embedding": embedding,
            },
        )
        await db.execute(stmt)
        stored = True

    return stored


async def retrieve_similar_patterns(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_question: str,
    top_k: int = 3,
) -> list[dict]:
    """Retrieve top-K similar proven query patterns for the given tenant.

    Returns list of dicts with 'question', 'sql', 'tables', 'success_count'.
    """
    embedding = await _embed_text(user_question)
    if embedding is None:
        return []

    # pgvector cosine distance: 1 - cosine_similarity
    # Lower distance = more similar
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    result = await db.execute(
        text("""
            SELECT user_question, working_sql, tables_used, success_count,
                   1 - (intent_embedding <=> CAST(:embedding AS vector)) as similarity
            FROM tenant_query_patterns
            WHERE tenant_id = CAST(:tenant_id AS uuid)
              AND intent_embedding IS NOT NULL
            ORDER BY intent_embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """),
        {
            "tenant_id": str(tenant_id),
            "embedding": embedding_str,
            "top_k": top_k,
        },
    )

    patterns = []
    for row in result.fetchall():
        patterns.append(
            {
                "question": row[0],
                "sql": row[1],
                "tables": row[2] or [],
                "success_count": row[3],
                "similarity": float(row[4]),
            }
        )

    return patterns
