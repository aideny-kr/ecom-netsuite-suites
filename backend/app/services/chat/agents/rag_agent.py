"""RAG / documentation search specialist agent.

Searches the vector store for relevant documentation, custom field
metadata, platform guides, and any other stored knowledge. Can perform
multi-query RAG by reformulating the search query if initial results
are poor.
"""

from __future__ import annotations

import uuid

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.tools import build_local_tool_definitions

# Tools this agent is allowed to use
_RAG_TOOL_NAMES = frozenset({"rag_search", "web_search", "tenant_save_learned_rule"})


class RAGAgent(BaseSpecialistAgent):
    """Specialist agent for documentation and knowledge base search."""

    def __init__(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        super().__init__(tenant_id, user_id, correlation_id)
        self._tool_defs: list[dict] | None = None

    @property
    def agent_name(self) -> str:
        return "rag"

    @property
    def max_steps(self) -> int:
        return 2  # rag search → web fallback (stop early if empty)

    @property
    def system_prompt(self) -> str:
        return (
            "You are a documentation and knowledge base search specialist. Your job is to find "
            "the most relevant information from stored documents or the web to answer the given task.\n"
            "\n"
            "LANGUAGE: Always respond in English only.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Use the rag_search tool to search stored documents first (1 call, up to 2 different queries max).\n"
            "2. If internal docs return 0 results, try ONE web_search call with a focused query.\n"
            "3. Return whatever you found. If nothing is found, say so immediately and STOP.\n"
            "\n"
            "CRITICAL — MINIMIZE TOOL CALLS:\n"
            "- You have a strict budget of 3-4 tool calls total. Do NOT exhaust them.\n"
            "- If both rag_search and web_search return 0 results, STOP IMMEDIATELY and report "
            '"No relevant documentation found." Do NOT retry with rephrased queries.\n'
            "- Never make more than 2 rag_search calls or 2 web_search calls.\n"
            "\n"
            "SEARCH TIPS:\n"
            "- For custom field lookups, search with terms like 'custbody', 'custcol', "
            "'custentity', 'custitem', or the field label.\n"
            "- Use source_filter='netsuite_metadata/' to narrow to custom field reference docs.\n"
            "- Use source_filter='netsuite_docs/' for SuiteQL syntax or record types.\n"
            "- Use source_filter='workspace_scripts/' to search SuiteScript source code and business logic.\n"
            "\n"
            "OUTPUT FORMAT:\n"
            "- Return the relevant information extracted from the documents.\n"
            "- Include the source_path for each piece of information.\n"
            "- Be concise — only include what's directly relevant to the task.\n"
            '- If no relevant results are found, say "No relevant documentation found" and stop.'
        )

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            all_tools = build_local_tool_definitions()
            self._tool_defs = [t for t in all_tools if t["name"] in _RAG_TOOL_NAMES]
        return self._tool_defs
