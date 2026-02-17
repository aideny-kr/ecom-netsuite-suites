import json
import re
import time
import uuid
from dataclasses import dataclass, field

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.mcp.server import mcp_server
from app.models.chat import DocChunk
from app.services.chat.embeddings import embed_query
from app.services.chat.prompts import (
    INPUT_SANITIZATION_PREFIX,
    ROUTER_PROMPT,
    SYSTEM_PROMPT,
    TABLE_SUMMARY_TEMPLATE,
)
from app.services.table_service import TABLE_MODEL_MAP

ALLOWED_CHAT_TOOLS: frozenset[str] = frozenset({
    "netsuite.suiteql_stub",
    "data.sample_table_read",
    "report.export",
})

SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@dataclass
class OrchestratorState:
    # Input
    user_message: str
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    session_id: uuid.UUID
    conversation_history: list[dict] = field(default_factory=list)
    # Intermediate
    route: dict | None = None
    doc_chunks: list[dict] | None = None
    db_results: dict | None = None
    tool_results: list[dict] | None = None
    # Output
    response: str | None = None
    citations: list[dict] | None = None
    tool_calls_log: list[dict] | None = None


def sanitize_user_input(text: str) -> str:
    """Strip potentially dangerous XML/prompt injection tags."""
    patterns = [
        r"</?\s*instructions\s*>",
        r"</?\s*system\s*>",
        r"</?\s*prompt\s*>",
        r"</?\s*context\s*>",
        r"</?\s*tool_call\s*>",
        r"</?\s*function_call\s*>",
    ]
    result = text
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    return result.strip()


def is_read_only_sql(query: str) -> bool:
    """Check if a SQL query is read-only (SELECT only)."""
    normalized = query.strip().upper()
    if not normalized.startswith("SELECT"):
        return False
    forbidden = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE"}
    # Simple token check — split on whitespace and check first token of each statement
    statements = normalized.split(";")
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        first_word = stmt.split()[0] if stmt.split() else ""
        if first_word in forbidden:
            return False
    return True


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def router_node(state: OrchestratorState) -> None:
    """Route the user question to appropriate data sources."""
    client = _get_anthropic_client()
    prompt = ROUTER_PROMPT.format(
        table_summary=TABLE_SUMMARY_TEMPLATE,
        user_message=sanitize_user_input(state.user_message),
    )
    try:
        response = await client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Extract JSON from possible markdown code blocks
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        state.route = json.loads(text)
    except (json.JSONDecodeError, IndexError, anthropic.APIError):
        state.route = {"needs_docs": True, "direct_answer": True}


async def retriever_node(state: OrchestratorState, db: AsyncSession) -> None:
    """Retrieve relevant doc chunks via vector similarity search."""
    if not state.route or not state.route.get("needs_docs"):
        return

    query_embedding = await embed_query(sanitize_user_input(state.user_message))
    top_k = settings.CHAT_RAG_TOP_K

    # pgvector cosine similarity search
    result = await db.execute(
        select(DocChunk)
        .where(
            (DocChunk.tenant_id == state.tenant_id) | (DocChunk.tenant_id == SYSTEM_TENANT_ID)
        )
        .order_by(DocChunk.embedding.cosine_distance(query_embedding))
        .limit(top_k)
    )
    chunks = result.scalars().all()

    if not chunks:
        # Keyword fallback
        search_term = f"%{state.user_message[:100]}%"
        result = await db.execute(
            select(DocChunk)
            .where(
                ((DocChunk.tenant_id == state.tenant_id) | (DocChunk.tenant_id == SYSTEM_TENANT_ID))
                & DocChunk.content.ilike(search_term)
            )
            .limit(top_k)
        )
        chunks = result.scalars().all()

    state.doc_chunks = [
        {"title": c.title, "content": c.content, "source_path": c.source_path}
        for c in chunks
    ]


async def db_reader_node(state: OrchestratorState, db: AsyncSession) -> None:
    """Read data from canonical tables based on router decision."""
    if not state.route or not state.route.get("needs_db"):
        return

    tables = state.route.get("db_tables", [])
    if not tables:
        return

    results = {}
    for table_name in tables[:3]:  # Max 3 tables
        model = TABLE_MODEL_MAP.get(table_name)
        if model is None:
            continue
        query = select(model).order_by(model.created_at.desc()).limit(20)
        result = await db.execute(query)
        rows = result.scalars().all()
        results[table_name] = [
            {k: str(v) for k, v in row.__dict__.items() if not k.startswith("_")}
            for row in rows
        ]

    state.db_results = results


async def tool_caller_node(
    state: OrchestratorState, db: AsyncSession, correlation_id: str
) -> None:
    """Call MCP tools if requested by router (read-only tools only)."""
    if not state.route or not state.route.get("needs_tool"):
        return

    tool_name = state.route.get("tool_name")
    tool_params = state.route.get("tool_params") or {}

    if not tool_name or tool_name not in ALLOWED_CHAT_TOOLS:
        state.tool_results = [
            {"tool": tool_name or "unknown", "error": f"Tool '{tool_name}' is not allowed in chat."}
        ]
        state.tool_calls_log = state.tool_results
        return

    start = time.monotonic()
    result = await mcp_server.call_tool(
        tool_name=tool_name,
        params=tool_params,
        tenant_id=str(state.tenant_id),
        actor_id=str(state.actor_id),
        correlation_id=correlation_id,
        db=db,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    tool_log = {
        "tool": tool_name,
        "params": tool_params,
        "result_summary": str(result)[:500],
        "duration_ms": duration_ms,
    }
    state.tool_results = [{"tool": tool_name, "result": result}]
    state.tool_calls_log = [tool_log]


async def synthesizer_node(state: OrchestratorState) -> None:
    """Synthesize final response from all gathered context."""
    client = _get_anthropic_client()

    # Build context block
    context_parts = []
    citations = []

    if state.doc_chunks:
        for chunk in state.doc_chunks:
            context_parts.append(f"[Documentation: {chunk['title']}]\n{chunk['content']}")
            citations.append({"type": "doc", "title": chunk["title"], "snippet": chunk["content"][:200]})

    if state.db_results:
        for table_name, rows in state.db_results.items():
            if rows:
                context_parts.append(f"[Table: {table_name}] ({len(rows)} rows)\n{json.dumps(rows[:5], indent=2)}")
                citations.append({"type": "table", "title": table_name, "snippet": f"{len(rows)} rows returned"})

    if state.tool_results:
        for tr in state.tool_results:
            tool_name = tr.get("tool", "unknown")
            result_str = str(tr.get("result", tr.get("error", "")))[:500]
            context_parts.append(f"[Tool: {tool_name}]\n{result_str}")

    context_block = "\n\n---\n\n".join(context_parts) if context_parts else "No additional context available."

    messages = list(state.conversation_history)
    messages.append({
        "role": "user",
        "content": f"{INPUT_SANITIZATION_PREFIX}\n\n<context>\n{context_block}\n</context>\n\nUser question: {sanitize_user_input(state.user_message)}",
    })

    response = await client.messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    state.response = response.content[0].text
    state.citations = citations if citations else None
