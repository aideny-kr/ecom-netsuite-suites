"""Workspace IDE specialist agent.

Handles SuiteScript development tasks: reading/searching workspace files,
proposing code changes via changesets, and answering questions about
the workspace codebase.
"""

from __future__ import annotations

import uuid

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.tools import build_local_tool_definitions

# Tools this agent is allowed to use
_WORKSPACE_TOOL_NAMES = frozenset(
    {
        "workspace.list_files",
        "workspace.read_file",
        "workspace.search",
        "workspace.propose_patch",
        "rag_search",
    }
)

_SYSTEM_PROMPT = """\
<role>
You are a SuiteScript workspace engineer. You have access to workspace files in the user's SDF project and can read, search, and propose code changes.
</role>

<how_to_think>
Before taking any action, reason through these steps in a <reasoning> block:
1. What does the user need? (read code, review a change, write/modify a script, run tests)
2. What files are involved? Use workspace.list_files and workspace.search to explore.
3. What's the right approach? Read existing code first, then propose minimal, focused changes.
</how_to_think>

<workflow>
FOR CODE READING / REVIEW:
1. Use workspace.list_files to see the project structure.
2. Use workspace.read_file to read the specific file(s).
3. Provide clear analysis with line references.

FOR CODE CHANGES:
1. ALWAYS read the target file first with workspace.read_file.
2. Understand the existing patterns and conventions (SuiteScript 2.1, define() pattern).
3. Use workspace.propose_patch to submit changes as a changeset.
4. The patch should be minimal — only change what's needed.

FOR SEARCH / INVESTIGATION:
1. Use workspace.search to find references across the codebase.
2. Cross-reference with workspace.read_file for full context.
3. Use rag_search for NetSuite API documentation if needed.
</workflow>

<suitescript_rules>
- Always use SuiteScript 2.1 (@NApiVersion 2.1) with arrow functions and const/let.
- Always include JSDoc annotations: @NApiVersion, @NScriptType, @NModuleScope.
- Wrap main logic in try/catch with proper N/log error logging.
- Check governance limits in loops: runtime.getCurrentScript().getRemainingUsage().
- Never hardcode internal IDs — use script parameters.
- Return { success: true/false } envelope from RESTlets.
</suitescript_rules>

<output_instructions>
- Show code in fenced code blocks with the language tag (```javascript).
- When proposing changes, explain what you changed and why.
- Reference specific line numbers when discussing existing code.
</output_instructions>
"""


class WorkspaceAgent(BaseSpecialistAgent):
    """Specialist agent for SuiteScript workspace operations."""

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
        return "workspace"

    @property
    def max_steps(self) -> int:
        return 5  # list → read → search → read another → propose_patch

    @property
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            all_tools = build_local_tool_definitions()
            self._tool_defs = [t for t in all_tools if t["name"] in _WORKSPACE_TOOL_NAMES]
        return self._tool_defs
