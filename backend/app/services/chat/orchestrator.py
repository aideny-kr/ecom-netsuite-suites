"""Agentic chat orchestrator using a multi-provider LLM adapter layer.

Supports Anthropic, OpenAI, and Gemini via the adapter pattern.
Claude decides which tools to call, sees results (including errors),
and can retry/correct — all within a single turn, up to MAX_STEPS iterations.
"""

import json
import logging
import re
import time
import uuid
from typing import Any

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
from app.services.chat.onboarding_tools import (
    ONBOARDING_TOOL_DEFINITIONS,
    execute_onboarding_tool,
)
from app.services.chat.prompts import INPUT_SANITIZATION_PREFIX, ONBOARDING_SYSTEM_PROMPT
from app.services.chat.tools import build_all_tool_definitions, execute_tool_call
from app.services.prompt_template_service import get_active_template

logger = logging.getLogger(__name__)

MAX_STEPS = settings.CHAT_MAX_TOOL_CALLS_PER_TURN  # default 5


def _extract_file_paths(nodes: list[dict]) -> list[str]:
    """Flatten a nested file tree into a sorted list of file paths."""
    paths: list[str] = []
    for node in nodes:
        if not node.get("is_directory"):
            paths.append(node.get("path", ""))
        children = node.get("children")
        if children:
            paths.extend(_extract_file_paths(children))
    return sorted(paths)


async def run_chat_turn(
    db: AsyncSession,
    session: ChatSession,
    user_message: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_msg: ChatMessage | None = None,
    wizard_step: str | None = None,
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

    is_onboarding = getattr(session, "session_type", "chat") == "onboarding"

    # ── Pre-loop: RAG retrieval for doc context (skip for onboarding) ──
    sanitized_input = sanitize_user_input(user_message)
    rag_context = ""
    citations: list[dict] = []

    if not is_onboarding:
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

    # ── Build tool definitions (with policy-based filtering) ──
    if is_onboarding:
        tool_definitions = list(ONBOARDING_TOOL_DEFINITIONS)
    else:
        tool_definitions = await build_all_tool_definitions(db, tenant_id)

    # ── Resolve tenant-specific system prompt ──
    if is_onboarding:
        from app.services.chat.prompts import ONBOARDING_STEP_CONTEXTS

        system_prompt = ONBOARDING_SYSTEM_PROMPT
        if wizard_step and wizard_step in ONBOARDING_STEP_CONTEXTS:
            system_prompt = (
                f"{ONBOARDING_SYSTEM_PROMPT}\n\n## Current Step: {wizard_step}\n{ONBOARDING_STEP_CONTEXTS[wizard_step]}"
            )
    else:
        system_prompt = await get_active_template(db, tenant_id)

    # ── Workspace context injection ──
    workspace_context: dict | None = None
    if getattr(session, "workspace_id", None):
        from app.services import workspace_service as ws_svc

        ws = await ws_svc.get_workspace(db, session.workspace_id, tenant_id)
        if ws:
            workspace_context = {
                "workspace_id": str(session.workspace_id),
                "name": ws.name,
            }
            files = await ws_svc.list_files(db, session.workspace_id, tenant_id)
            file_paths = _extract_file_paths(files)
            file_listing = "\n".join(f"- {p}" for p in file_paths[:50])
            if len(file_paths) > 50:
                file_listing += f"\n... and {len(file_paths) - 50} more files"
            system_prompt += (
                f"\n\nWORKSPACE CONTEXT:\n"
                f"Active workspace: '{ws.name}' (ID: {session.workspace_id}).\n"
                f"Files in workspace:\n{file_listing}\n\n"
                f"Use workspace tools (workspace_list_files, workspace_read_file, "
                f"workspace_search, workspace_propose_patch) to browse and modify files. "
                f"The workspace_id is '{session.workspace_id}' — it will be auto-injected."
            )

    # ── Load active policy for tool gating + output redaction ──
    from app.services.policy_service import evaluate_tool_call as policy_evaluate
    from app.services.policy_service import get_active_policy, redact_output

    active_policy = await get_active_policy(db, tenant_id)

    # ── Resolve tenant AI config ──
    provider, model, api_key, is_byok = await get_tenant_ai_config(db, tenant_id)
    adapter = get_adapter(provider, api_key)

    # ── Multi-agent routing (opt-in per tenant or globally) ──
    if not is_onboarding and not workspace_context:
        from sqlalchemy import select as sa_select

        from app.models.tenant import TenantConfig

        use_multi_agent = settings.MULTI_AGENT_ENABLED
        try:
            tc_result = await db.execute(
                sa_select(TenantConfig.multi_agent_enabled).where(TenantConfig.tenant_id == tenant_id)
            )
            tenant_ma = tc_result.scalar_one_or_none()
            if isinstance(tenant_ma, bool):
                use_multi_agent = tenant_ma
        except Exception:
            pass  # Fall through to single-agent

        if use_multi_agent:
            from app.services.chat.coordinator import MultiAgentCoordinator
            from app.services.chat.llm_adapter import get_adapter as get_specialist_adapter
            from app.services.netsuite_metadata_service import get_active_metadata

            specialist_adapter = get_specialist_adapter(
                settings.MULTI_AGENT_SPECIALIST_PROVIDER,
                api_key if settings.MULTI_AGENT_SPECIALIST_PROVIDER == provider else settings.ANTHROPIC_API_KEY,
            )
            metadata = await get_active_metadata(db, tenant_id)

            coordinator = MultiAgentCoordinator(
                db=db,
                tenant_id=tenant_id,
                user_id=user_id,
                correlation_id=correlation_id,
                main_adapter=adapter,
                main_model=model,
                specialist_adapter=specialist_adapter,
                specialist_model=settings.MULTI_AGENT_SPECIALIST_MODEL,
                metadata=metadata,
                policy=active_policy,
                system_prompt=system_prompt,
            )

            coord_result = await coordinator.run(
                user_message=sanitized_input,
                conversation_history=history_messages,
                rag_context=rag_context,
            )

            final_text = re.sub(r"\s*\[tool:\s*[^\]]+\]", "", coord_result.final_text).strip()

            assistant_msg = ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content=final_text or "I'm sorry, I couldn't generate a response.",
                tool_calls=coord_result.tool_calls_log if coord_result.tool_calls_log else None,
                citations=citations if citations else None,
                token_count=coord_result.total_input_tokens + coord_result.total_output_tokens,
                input_tokens=coord_result.total_input_tokens,
                output_tokens=coord_result.total_output_tokens,
                model_used=model,
                provider_used=provider,
                is_byok=is_byok,
            )
            db.add(assistant_msg)

            if not session.title:
                session.title = user_message[:100].strip()

            audit_payload: dict[str, Any] = {
                "mode": "multi_agent",
                "provider": provider,
                "model": model,
                "specialist_model": settings.MULTI_AGENT_SPECIALIST_MODEL,
                "steps": len(coord_result.tool_calls_log),
                "input_tokens": coord_result.total_input_tokens,
                "output_tokens": coord_result.total_output_tokens,
                "doc_chunks_count": len(state.doc_chunks) if state.doc_chunks else 0,
                "tools_called": [t["tool"] for t in coord_result.tool_calls_log],
            }
            await log_event(
                db=db,
                tenant_id=tenant_id,
                category="chat",
                action="chat.turn",
                actor_id=user_id,
                resource_type="chat_session",
                resource_id=str(session.id),
                correlation_id=correlation_id,
                payload=audit_payload,
            )

            await db.commit()
            return assistant_msg

    # ── Single-agent agentic loop (default path) ──
    tool_calls_log: list[dict] = []
    final_text = ""
    total_input_tokens = 0
    total_output_tokens = 0

    for step in range(MAX_STEPS):
        response = await adapter.create_message(
            model=model,
            max_tokens=4096,
            system=system_prompt,
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
            # Auto-inject workspace_id for workspace tools
            if workspace_context and block.name.startswith("workspace_"):
                if "workspace_id" not in block.input:
                    block.input["workspace_id"] = workspace_context["workspace_id"]

            # Route tool execution: onboarding tools vs standard tools
            t0 = time.monotonic()
            if is_onboarding and block.name in (
                "save_onboarding_profile",
                "start_netsuite_oauth",
                "check_netsuite_connection",
            ):
                result_str = await execute_onboarding_tool(
                    tool_name=block.name,
                    tool_input=block.input,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    db=db,
                )
            else:
                # Policy evaluation: check if tool call is allowed
                policy_result = policy_evaluate(active_policy, block.name, block.input)
                if not policy_result["allowed"]:
                    result_str = json.dumps({"error": f"Policy blocked: {policy_result.get('reason', 'Not allowed')}"})
                else:
                    result_str = await execute_tool_call(
                        tool_name=block.name,
                        tool_input=block.input,
                        tenant_id=tenant_id,
                        actor_id=user_id,
                        correlation_id=correlation_id,
                        db=db,
                    )

                    # Output redaction: strip blocked fields from tool results
                    if active_policy and active_policy.blocked_fields:
                        try:
                            parsed = json.loads(result_str)
                            parsed = redact_output(active_policy, parsed)
                            result_str = json.dumps(parsed, default=str)
                        except (json.JSONDecodeError, TypeError):
                            pass  # Non-JSON result, skip redaction

            tool_results_content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "tool_name": block.name,
                    "content": result_str,
                }
            )

            # Log for audit
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            tool_calls_log.append(
                {
                    "step": step,
                    "tool": block.name,
                    "params": block.input,
                    "result_summary": result_str[:500],
                    "duration_ms": elapsed_ms,
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
            system=system_prompt,
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
    audit_payload: dict[str, Any] = {
        "mode": "agentic",
        "provider": provider,
        "model": model,
        "steps": len(tool_calls_log),
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "doc_chunks_count": 0 if is_onboarding else (len(state.doc_chunks) if state.doc_chunks else 0),
        "tools_called": [t["tool"] for t in tool_calls_log],
    }
    if workspace_context:
        audit_payload["workspace_id"] = workspace_context["workspace_id"]
    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="chat",
        action="chat.turn",
        actor_id=user_id,
        resource_type="chat_session",
        resource_id=str(session.id),
        correlation_id=correlation_id,
        payload=audit_payload,
    )

    await db.commit()
    return assistant_msg
