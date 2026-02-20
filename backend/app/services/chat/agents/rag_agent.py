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
_RAG_TOOL_NAMES = frozenset({"rag_search", "web_search"})


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
        return 3  # rag search → refine → web fallback

    @property
    def system_prompt(self) -> str:
        return (
            "You are a documentation and knowledge base search specialist. Your job is to find "
            "the most relevant information from stored documents or the web to answer the given task.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Use the rag_search tool to search stored documents first.\n"
            "2. Review the results. If they don't contain what you need, try a different "
            "search query with alternative keywords or a more specific/broader phrasing.\n"
            "3. If internal docs are insufficient, use web_search to find external information.\n"
            "4. Return the relevant excerpts with clear citations.\n"
            "\n"
            "SEARCH TIPS:\n"
            "- For custom field lookups, search with terms like 'custbody', 'custcol', "
            "'custentity', 'custitem', or the field label.\n"
            "- For SuiteQL syntax or record types, use source_filter='netsuite_docs/' "
            "to narrow to NetSuite reference documentation.\n"
            "- Use source_filter='netsuite_metadata/' to narrow to custom field reference docs.\n"
            "- For unfamiliar error messages or recent NetSuite features, use web_search.\n"
            "\n"
            "OUTPUT FORMAT:\n"
            "- Return the relevant information extracted from the documents.\n"
            "- Include the source_path for each piece of information.\n"
            "- For web results, include the URL.\n"
            "- Be concise — only include what's directly relevant to the task.\n"
            "- If no relevant results are found, say so clearly."
        )

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            all_tools = build_local_tool_definitions()
            self._tool_defs = [t for t in all_tools if t["name"] in _RAG_TOOL_NAMES]
        return self._tool_defs
