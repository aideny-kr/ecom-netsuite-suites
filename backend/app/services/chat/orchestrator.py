import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.nodes import (
    OrchestratorState,
    db_reader_node,
    retriever_node,
    router_node,
    sanitize_user_input,
    synthesizer_node,
    tool_caller_node,
)


async def run_chat_turn(
    db: AsyncSession,
    session: ChatSession,
    user_message: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> ChatMessage:
    """Execute a full chat turn: route -> retrieve -> read DB -> call tools -> synthesize."""
    correlation_id = str(uuid.uuid4())

    # Load conversation history (last N turns)
    max_turns = settings.CHAT_MAX_HISTORY_TURNS
    history_messages = []
    if session.messages:
        recent = session.messages[-(max_turns * 2):]
        for msg in recent:
            if msg.role in ("user", "assistant"):
                history_messages.append({"role": msg.role, "content": msg.content})

    # Save user message
    user_msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=session.id,
        role="user",
        content=user_message,
    )
    db.add(user_msg)
    await db.flush()

    # Build state
    state = OrchestratorState(
        user_message=sanitize_user_input(user_message),
        tenant_id=tenant_id,
        actor_id=user_id,
        session_id=session.id,
        conversation_history=history_messages,
    )

    # Execute pipeline
    await router_node(state)
    await retriever_node(state, db)
    await db_reader_node(state, db)
    await tool_caller_node(state, db, correlation_id)
    await synthesizer_node(state)

    # Save assistant message
    assistant_msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=session.id,
        role="assistant",
        content=state.response or "I'm sorry, I couldn't generate a response.",
        tool_calls=state.tool_calls_log,
        citations=state.citations,
    )
    db.add(assistant_msg)

    # Auto-title from first message
    if not session.title:
        session.title = user_message[:100].strip()

    # Audit
    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="chat",
        action="chat.turn",
        actor_id=user_id,
        resource_type="chat_session",
        resource_id=str(session.id),
        correlation_id=correlation_id,
        payload={
            "route": state.route,
            "doc_chunks_count": len(state.doc_chunks) if state.doc_chunks else 0,
            "db_tables": list(state.db_results.keys()) if state.db_results else [],
            "tools_called": [t["tool"] for t in state.tool_calls_log] if state.tool_calls_log else [],
        },
    )

    await db.commit()
    return assistant_msg
