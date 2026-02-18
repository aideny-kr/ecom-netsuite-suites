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
_RAG_TOOL_NAMES = frozenset({"rag_search"})


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
        return 2  # search → refine if needed

    @property
    def system_prompt(self) -> str:
        return (
            "You are a documentation and knowledge base search specialist. Your job is to find "
            "the most relevant information from stored documents to answer the given task.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Use the rag_search tool to search for relevant documents.\n"
            "2. Review the results. If they don't contain what you need, try a different "
            "search query with alternative keywords or a more specific/broader phrasing.\n"
            "3. Return the relevant excerpts with clear citations.\n"
            "\n"
            "SEARCH TIPS:\n"
            "- For custom field lookups, search with terms like 'custbody', 'custcol', "
            "'custentity', 'custitem', or the field label.\n"
            "- For NetSuite documentation, search with specific feature names.\n"
            "- Use source_filter='netsuite_metadata/' to narrow to custom field reference docs.\n"
            "- You can search up to 2 times if the first results are not relevant.\n"
            "\n"
            "OUTPUT FORMAT:\n"
            "- Return the relevant information extracted from the documents.\n"
            "- Include the source_path for each piece of information.\n"
            "- Be concise — only include what's directly relevant to the task.\n"
            "- If no relevant results are found, say so clearly."
        )

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            all_tools = build_local_tool_definitions()
            self._tool_defs = [t for t in all_tools if t["name"] in _RAG_TOOL_NAMES]
        return self._tool_defs
