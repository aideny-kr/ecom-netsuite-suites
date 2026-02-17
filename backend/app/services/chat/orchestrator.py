"""Agentic chat orchestrator using a multi-provider LLM adapter layer.

Supports Anthropic, OpenAI, and Gemini via the adapter pattern.
Claude decides which tools to call, sees results (including errors),
and can retry/correct — all within a single turn, up to MAX_STEPS iterations.
"""

import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.llm_adapter import get_adapter
from app.services.chat.nodes import (
    OrchestratorState,
    get_tenant_ai_config,
    retriever_node,
    sanitize_user_input,
)
from app.services.chat.prompts import AGENTIC_SYSTEM_PROMPT, INPUT_SANITIZATION_PREFIX
from app.services.chat.tools import build_all_tool_definitions, execute_tool_call

logger = logging.getLogger(__name__)

MAX_STEPS = settings.CHAT_MAX_TOOL_CALLS_PER_TURN  # default 5


async def run_chat_turn(
    db: AsyncSession,
    session: ChatSession,
    user_message: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_msg: ChatMessage | None = None,
) -> ChatMessage:
    """Execute an agentic chat turn with Claude's native tool use.

    Signature and return type match the previous linear pipeline —
    chat.py needs zero changes.
    """
    correlation_id = str(uuid.uuid4())

    # ── Load conversation history ──
    max_turns = settings.CHAT_MAX_HISTORY_TURNS
    history_messages: list[dict] = []
    if session.messages:
        recent = session.messages[-(max_turns * 2) :]
        for msg in recent:
            if msg.role in ("user", "assistant"):
                history_messages.append({"role": msg.role, "content": msg.content})

    # ── Save user message (if not already saved by caller) ──
    if user_msg is None:
        user_msg = ChatMessage(
            tenant_id=tenant_id,
            session_id=session.id,
            role="user",
            content=user_message,
        )
        db.add(user_msg)
        await db.flush()

    # ── Pre-loop: RAG retrieval for doc context ──
    sanitized_input = sanitize_user_input(user_message)
    state = OrchestratorState(
        user_message=sanitized_input,
        tenant_id=tenant_id,
        actor_id=user_id,
        session_id=session.id,
        conversation_history=history_messages,
        route={"needs_docs": True},  # always attempt RAG
    )
    await retriever_node(state, db)

    # Build RAG context block
    rag_context = ""
    citations: list[dict] = []
    if state.doc_chunks:
        rag_parts = []
        for chunk in state.doc_chunks:
            rag_parts.append(f"[Documentation: {chunk['title']}]\n{chunk['content']}")
            citations.append(
                {
                    "type": "doc",
                    "title": chunk["title"],
                    "snippet": chunk["content"][:200],
                }
            )
        rag_context = "\n\n".join(rag_parts)

    # ── Build messages for Claude ──
    messages: list[dict] = list(history_messages)

    # Compose user message with sanitization prefix and RAG context
    user_content = f"{INPUT_SANITIZATION_PREFIX}\n\n"
    if rag_context:
        user_content += f"<context>\n{rag_context}\n</context>\n\n"
    user_content += f"User question: {sanitized_input}"
    messages.append({"role": "user", "content": user_content})

    # ── Build tool definitions ──
    tool_definitions = await build_all_tool_definitions(db, tenant_id)

    # ── Resolve tenant AI config ──
    provider, model, api_key, is_byok = await get_tenant_ai_config(db, tenant_id)
    adapter = get_adapter(provider, api_key)

    # ── Agentic loop ──
    tool_calls_log: list[dict] = []
    final_text = ""
    total_input_tokens = 0
    total_output_tokens = 0

    for step in range(MAX_STEPS):
        response = await adapter.create_message(
            model=model,
            max_tokens=4096,
            system=AGENTIC_SYSTEM_PROMPT,
            messages=messages,
            tools=tool_definitions if tool_definitions else None,
        )
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        if not response.tool_use_blocks:
            # Pure text response — we're done
            final_text = "\n".join(response.text_blocks) if response.text_blocks else ""
            break

        # Append assistant message
        messages.append(adapter.build_assistant_message(response))

        # Execute each tool call and collect results
        tool_results_content = []
        for block in response.tool_use_blocks:
            result_str = await execute_tool_call(
                tool_name=block.name,
                tool_input=block.input,
                tenant_id=tenant_id,
                actor_id=user_id,
                correlation_id=correlation_id,
                db=db,
            )

            tool_results_content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "tool_name": block.name,
                    "content": result_str,
                }
            )

            # Log for audit
            tool_calls_log.append(
                {
                    "step": step,
                    "tool": block.name,
                    "params": block.input,
                    "result_summary": result_str[:500],
                }
            )

        messages.append(adapter.build_tool_result_message(tool_results_content))

    else:
        # Loop exhausted — make one final call without tools to force text
        logger.warning(
            "Agentic loop exhausted %d steps, forcing final response",
            MAX_STEPS,
        )
        response = await adapter.create_message(
            model=model,
            max_tokens=2048,
            system=AGENTIC_SYSTEM_PROMPT,
            messages=messages,
        )
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        final_text = "\n".join(response.text_blocks) if response.text_blocks else ""

    # Strip raw tool reference tags the LLM may include in its text output
    final_text = re.sub(r"\s*\[tool:\s*[^\]]+\]", "", final_text).strip()

    # ── Save assistant message ──
    assistant_msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=session.id,
        role="assistant",
        content=final_text or "I'm sorry, I couldn't generate a response.",
        tool_calls=tool_calls_log if tool_calls_log else None,
        citations=citations if citations else None,
        token_count=total_input_tokens + total_output_tokens,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        model_used=model,
        provider_used=provider,
        is_byok=is_byok,
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
            "mode": "agentic",
            "provider": provider,
            "model": model,
            "steps": len(tool_calls_log),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "doc_chunks_count": len(state.doc_chunks) if state.doc_chunks else 0,
            "tools_called": [t["tool"] for t in tool_calls_log],
        },
    )

    await db.commit()
    return assistant_msg
