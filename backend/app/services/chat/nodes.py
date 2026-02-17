"""Chat support nodes — utilities and retriever for the agentic orchestrator.

Kept:
- OrchestratorState (used by retriever_node)
- sanitize_user_input(), is_read_only_sql(), get_tenant_ai_config()
- retriever_node() (runs before agentic loop)
- ALLOWED_CHAT_TOOLS, SYSTEM_TENANT_ID constants

Removed (replaced by Claude's native tool_use in orchestrator.py):
- router_node, synthesizer_node, tool_caller_node, db_reader_node
- _call_local_tool, _call_external_tool, _build_external_tools_summary
"""

import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import decrypt_credentials
from app.models.chat import DocChunk
from app.models.tenant import TenantConfig
from app.services.chat.embeddings import embed_query
from app.services.chat.llm_adapter import DEFAULT_MODELS

logger = logging.getLogger(__name__)

ALLOWED_CHAT_TOOLS: frozenset[str] = frozenset({
    "netsuite.suiteql",
    "netsuite.connectivity",
    "data.sample_table_read",
    "report.export",
    "workspace.list_files",
    "workspace.read_file",
    "workspace.search",
    "workspace.propose_patch",
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
    external_tool_results: list[dict] | None = None
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
    statements = normalized.split(";")
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        first_word = stmt.split()[0] if stmt.split() else ""
        if first_word in forbidden:
            return False
    return True


async def get_tenant_ai_config(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[str, str, str, bool]:
    """Return (provider, model, api_key, is_byok) with fallback to platform defaults."""
    result = await db.execute(
        select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
    )
    config = result.scalar_one_or_none()

    if config and config.ai_provider and config.ai_api_key_encrypted:
        key = decrypt_credentials(config.ai_api_key_encrypted)["api_key"]
        model = config.ai_model or DEFAULT_MODELS.get(config.ai_provider, "")
        return config.ai_provider, model, key, True

    # Platform defaults
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("No AI provider configured — set a tenant API key or ANTHROPIC_API_KEY")
    return "anthropic", settings.ANTHROPIC_MODEL, settings.ANTHROPIC_API_KEY, False


async def retriever_node(state: OrchestratorState, db: AsyncSession) -> None:
    """Retrieve relevant doc chunks via vector similarity search."""
    if not state.route or not state.route.get("needs_docs"):
        return

    try:
        query_embedding = await embed_query(sanitize_user_input(state.user_message))
        if query_embedding is None:
            state.doc_chunks = []
            return
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
    except Exception:
        logger.warning("retriever_node failed, continuing without docs", exc_info=True)
        state.doc_chunks = []
