"""Workspace IDE specialist agent.

Handles SuiteScript development tasks: reading/searching workspace files,
proposing code changes via changesets, and answering questions about
the workspace codebase.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Iterable

from app.models.workspace import ValidationHit
from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.tools import build_local_tool_definitions
from app.services.workspace.auto_validate_orchestrator import get_orchestrator
from app.services.workspace.mechanical_fix_classifier import classify

# Tools this agent is allowed to use
_WORKSPACE_TOOL_NAMES = frozenset(
    {
        "workspace_list_files",
        "workspace_read_file",
        "workspace_search",
        "workspace_propose_patch",
        "workspace_run_validate",
        "rag_search",
        "tenant_save_learned_rule",
    }
)

# How many error families the agent should narrate before deferring to the
# runs panel. Referenced in the system prompt (post_validate_workflow block).
_HIT_FAMILY_CITATION_CAP = 3

_SYSTEM_PROMPT = f"""\
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
- Return {{ success: true/false }} envelope from RESTlets.
</suitescript_rules>

<post_validate_workflow>
WHEN VALIDATE RESULTS ARE INJECTED INTO THE CONVERSATION:
1. The system has already grouped hits by code family. ONE narration per family.
2. For EACH family: pull a citation from the appropriate oracle/* RAG partition
   (ai-connector, owasp, sdf-docs, sdf-roles, records, upgrade, uif-spa) using
   rag_search. Cite the partition + chunk in the narration.
3. Limit narration to {_HIT_FAMILY_CITATION_CAP} families MAX. If more families,
   say "X additional warnings — see the runs panel for details" rather than
   narrating all.
4. The system has already auto-proposed fixes for mechanically-fixable hits. Do
   NOT propose fixes manually for OWASP, governance, or architectural hits —
   narrate only and let the user decide.
</post_validate_workflow>

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


def _batch_hits_by_family(hits: Iterable[ValidationHit]) -> dict[str, list[ValidationHit]]:
    """Group hits by code so the agent narrates one citation per family (codex #8)."""
    families: dict[str, list[ValidationHit]] = defaultdict(list)
    for hit in hits:
        key = hit.code or "UNCODED"
        families[key].append(hit)
    return dict(families)


async def _maybe_auto_propose_fix(
    *,
    hit: ValidationHit,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """If the hit's code is in the mechanical-fix allowlist AND we're under budget,
    enqueue a draft fix patch via workspace_propose_patch.

    Codex #10: deny-by-default. The classifier is the only gate.

    Note: the plan named this tool `workspace_propose_patch`, but the actual
    callable in `app.mcp.tools.workspace_tools` is `execute_propose_patch`.
    Using `execute_propose_patch` (the function that exists at the time of
    writing); Task 8 may rewire dispatch later.
    """
    fix = classify(code=hit.code, message=hit.message, file_path=hit.file_path, line=hit.line)
    if fix is None:
        return

    orch = get_orchestrator()
    if not orch.under_budget(changeset_id):
        return
    if not orch.should_auto_propose(changeset_id, hit.fingerprint):
        return

    from app.mcp.tools import workspace_tools  # local import to avoid cycle

    await workspace_tools.execute_propose_patch(
        params={
            "title": f"Auto-fix: {fix.replacement_summary}",
            "rule_id": fix.rule_id,
            "target_file": hit.file_path,
            "target_line": hit.line,
        },
        context={"tenant_id": str(tenant_id), "user_id": str(user_id)},
    )
    orch.record_auto_propose(changeset_id, hit.fingerprint)
    orch.record_auto_fix(changeset_id)
