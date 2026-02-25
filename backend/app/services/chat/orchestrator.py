"""Agentic chat orchestrator using a multi-provider LLM adapter layer.

Supports Anthropic, OpenAI, and Gemini via the adapter pattern.
Claude decides which tools to call, sees results (including errors),
and can retry/correct — all within a single turn, up to MAX_STEPS iterations.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.billing import deduct_chat_credits
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


def _sanitize_for_prompt(text: str) -> str:
    """Strip control characters and limit length for prompt injection safety."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return cleaned[:500]


def _is_valid_uuid(val: str) -> bool:
    """Check if a string is a valid UUID."""
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError):
        return False


async def _resolve_default_workspace(
    db: AsyncSession, tenant_id: uuid.UUID,
) -> str | None:
    """Find the most recent active workspace for a tenant (cached per request)."""
    from sqlalchemy import select

    from app.models.workspace import Workspace

    result = await db.execute(
        select(Workspace.id)
        .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
        .order_by(Workspace.created_at.desc())
        .limit(1)
    )
    ws = result.scalar_one_or_none()
    return str(ws) if ws else None


async def _dispatch_memory_update(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    user_message: str,
    assistant_message: str,
) -> None:
    """Helper to safely run the regex-gated memory updater in the background."""
    from app.services.chat.llm_adapter import get_adapter
    from app.services.chat.memory_updater import maybe_extract_correction

    provider = settings.MULTI_AGENT_SPECIALIST_PROVIDER
    api_key = settings.ANTHROPIC_API_KEY
    model = settings.MULTI_AGENT_SPECIALIST_MODEL

    try:
        adapter = get_adapter(provider, api_key)
        await maybe_extract_correction(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            user_message=user_message,
            assistant_message=assistant_message,
            adapter=adapter,
            model=model,
        )
    except Exception as e:
        logger.error(f"background.memory_update_failed: {e}")


async def run_chat_turn(
    db: AsyncSession,
    session: ChatSession,
    user_message: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_msg: ChatMessage | None = None,
    wizard_step: str | None = None,
    user_timezone: str | None = None,
) -> AsyncGenerator[dict, None]:
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

    # ── Compact history if too long (saves tokens on subsequent calls) ──
    if len(history_messages) > 12:
        try:
            from app.services.chat.history_compactor import compact_history
            from app.services.chat.llm_adapter import get_adapter as _get_compactor_adapter

            compactor_adapter = _get_compactor_adapter(
                settings.MULTI_AGENT_SPECIALIST_PROVIDER,
                settings.ANTHROPIC_API_KEY,
            )
            history_messages = await compact_history(
                history_messages,
                adapter=compactor_adapter,
                model=settings.MULTI_AGENT_SPECIALIST_MODEL,
            )
        except Exception:
            logger.warning("history_compaction_failed", exc_info=True)

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
        try:
            await retriever_node(state, db)
        except Exception:
            logger.warning("Pre-loop RAG retrieval failed, continuing without docs")
            state.doc_chunks = []

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

    # ── Inject AI Soul (Tone & Quirks) ──
    soul_bot_tone = ""
    if not is_onboarding:
        from app.services.soul_service import get_soul_config
        soul_config = await get_soul_config(tenant_id)
        if soul_config.exists:
            soul_parts = ["\n\n## Tenant-Specific AI Configuration & Logic\n"]
            if soul_config.bot_tone:
                soul_bot_tone = soul_config.bot_tone
                soul_parts.append(f"TONE & MANNER:\n{soul_config.bot_tone}\n")
            if soul_config.netsuite_quirks:
                soul_parts.append(f"NETSUITE QUIRKS & LOGIC:\n{soul_config.netsuite_quirks}\n")
            system_prompt += "\n".join(soul_parts)

    # ── Inject dynamic tool inventory into system prompt ──
    # This ensures the model always knows the exact tool names it can call,
    # regardless of whether the base prompt matches.
    if not is_onboarding and tool_definitions:
        tool_inventory_lines = ["\nAVAILABLE TOOLS (use these exact names when calling tools):"]
        ext_suiteql_tools = []
        for td in tool_definitions:
            tool_inventory_lines.append(f"- {td['name']}: {td.get('description', '')}")
            # Detect external MCP tools that can run SuiteQL
            if td["name"].startswith("ext__") and "suiteql" in td["name"].lower():
                ext_suiteql_tools.append(td["name"])

        # If external MCP SuiteQL tools are available, guide the LLM to prefer them
        if ext_suiteql_tools:
            tool_inventory_lines.append(
                "\n\nIMPORTANT — NETSUITE MCP TOOLS:\n"
                "You have access to NetSuite's native MCP tools (prefixed with 'ext__'). "
                "These connect DIRECTLY to NetSuite and are more reliable than the local "
                "netsuite_suiteql tool.\n"
                "\n"
                "PREFERRED TOOL FOR SUITEQL QUERIES:\n"
                f"Use `{ext_suiteql_tools[0]}` instead of `netsuite_suiteql` for all "
                "SuiteQL data queries. This tool runs queries directly inside NetSuite "
                "via the MCP protocol.\n"
                'Parameters: {"sqlQuery": "SELECT ...", "description": "Brief description"}\n'
                "\n"
                "Only fall back to `netsuite_suiteql` if the MCP tool is unavailable or errors."
            )

        system_prompt += "\n".join(tool_inventory_lines)

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
            file_listing = "\n".join(f"- {_sanitize_for_prompt(p)}" for p in file_paths[:50])
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
            system_prompt += (
                "\nWhen the user mentions they are 'viewing' or 'looking at' a specific file, "
                "or when the message includes '[Currently viewing file: ...]', "
                "use the workspace_read_file tool to read that file's content before responding. "
                "This lets you see exactly what the user sees in their editor."
            )
            system_prompt += (
                "\n\n## IDE Chat Behavior\n"
                "You are an IDE assistant with direct file access. "
                "Be concise — lead with the answer, use code blocks, no preambles.\n"
                "For complex reasoning, use <thinking>...</thinking> tags before your answer. "
                "This block is collapsed by default in the UI.\n"
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
                user_timezone=user_timezone,
            )
            coordinator.soul_tone = soul_bot_tone

            # Stream multi-agent: dispatch agents first, then stream synthesis
            streamed_text_parts: list[str] = []
            async for event in coordinator.run_streaming(
                user_message=sanitized_input,
                conversation_history=history_messages,
                rag_context=rag_context,
            ):
                if event["type"] == "text":
                    streamed_text_parts.append(event["content"])
                yield event
                if event["type"] == "message":
                    # Final message already yielded — save and return
                    break

            coord_result = coordinator.last_result
            if coord_result is None:
                # Fallback: synthesis didn't produce a result
                final_text = (
                    "".join(streamed_text_parts).strip()
                    or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?"
                )
                coord_result_tokens = (0, 0)
                coord_result_tool_calls: list[dict] = []
            else:
                final_text = re.sub(r"\s*\[tool:\s*[^\]]+\]", "", coord_result.final_text).strip()
                coord_result_tokens = (coord_result.total_input_tokens, coord_result.total_output_tokens)
                coord_result_tool_calls = coord_result.tool_calls_log

            assistant_msg = ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content=final_text
                or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?",
                tool_calls=coord_result_tool_calls if coord_result_tool_calls else None,
                citations=citations if citations else None,
                token_count=coord_result_tokens[0] + coord_result_tokens[1],
                input_tokens=coord_result_tokens[0],
                output_tokens=coord_result_tokens[1],
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
                "steps": len(coord_result_tool_calls),
                "input_tokens": coord_result_tokens[0],
                "output_tokens": coord_result_tokens[1],
                "doc_chunks_count": len(state.doc_chunks) if state.doc_chunks else 0,
                "tools_called": [t["tool"] for t in coord_result_tool_calls],
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

            # Tollbooth: deduct credits before commit (skip for BYOK users)
            if not is_byok:
                await deduct_chat_credits(db, tenant_id, model)

            await db.commit()

            # Fire-and-forget background memory update
            asyncio.create_task(
                _dispatch_memory_update(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    db=db,
                    user_message=sanitized_input,
                    assistant_message=final_text,
                )
            )

            # If we already yielded the final message via streaming, just return
            # Otherwise yield a final message now
            if not streamed_text_parts:
                result_msg = {
                    "id": str(assistant_msg.id),
                    "role": assistant_msg.role,
                    "content": assistant_msg.content,
                    "tool_calls": assistant_msg.tool_calls,
                    "citations": assistant_msg.citations,
                }
                if hasattr(assistant_msg, "created_at") and assistant_msg.created_at:
                    result_msg["created_at"] = assistant_msg.created_at.isoformat()
                yield {"type": "message", "message": result_msg}
            return

    # ── Single-agent agentic loop (default path) ──
    tool_calls_log: list[dict] = []
    final_text = ""
    total_input_tokens = 0
    total_output_tokens = 0

    for step in range(MAX_STEPS):
        response = None

        # Determine if we should stream using stream_message or fallback
        # (Assuming all adapters implemented stream_message, else this would fail)
        async for event_type, payload in adapter.stream_message(
            model=model,
            max_tokens=16384,
            system=system_prompt,
            messages=messages,
            tools=tool_definitions if tool_definitions else None,
        ):
            if event_type == "text":
                yield {"type": "text", "content": payload}
            elif event_type == "response":
                response = payload

        if not response:
            break

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
            if block.name.startswith("workspace_"):
                if "workspace_id" not in block.input or not _is_valid_uuid(block.input.get("workspace_id", "")):
                    if workspace_context:
                        block.input["workspace_id"] = workspace_context["workspace_id"]
                    elif not workspace_context:
                        # Auto-resolve default workspace for non-workspace sessions
                        resolved_ws_id = await _resolve_default_workspace(db, tenant_id)
                        if resolved_ws_id:
                            block.input["workspace_id"] = resolved_ws_id
                            if not workspace_context:
                                workspace_context = {"workspace_id": resolved_ws_id, "name": "auto-resolved"}

            yield {"type": "tool_status", "content": f"Executing {block.name}..."}

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
        response = None
        async for event_type, payload in adapter.stream_message(
            model=model,
            max_tokens=8192,
            system=system_prompt,
            messages=messages,
        ):
            if event_type == "text":
                yield {"type": "text", "content": payload}
            elif event_type == "response":
                response = payload

        if response:
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
        content=final_text
        or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?",
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

    # Tollbooth: deduct credits before commit (skip for BYOK users)
    if not is_byok:
        await deduct_chat_credits(db, tenant_id, model)

    await db.commit()

    # Fire-and-forget background memory update
    asyncio.create_task(
        _dispatch_memory_update(
            tenant_id=tenant_id,
            user_id=user_id,
            db=db,
            user_message=sanitized_input,
            assistant_message=final_text,
        )
    )

    result_msg = {
        "id": str(assistant_msg.id),
        "role": assistant_msg.role,
        "content": assistant_msg.content,
        "tool_calls": assistant_msg.tool_calls,
        "citations": assistant_msg.citations,
    }
    if hasattr(assistant_msg, "created_at") and assistant_msg.created_at:
        result_msg["created_at"] = assistant_msg.created_at.isoformat()

    yield {"type": "message", "message": result_msg}
