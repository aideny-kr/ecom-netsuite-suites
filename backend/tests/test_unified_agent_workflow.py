"""Tests for unified agent workflow (restructured prompt — XML sections).

Validates that the unified agent prompt has the battle-tested workflow,
anti-enrichment rules, correct RMA status codes, and investigation mode.
"""

import uuid

from app.services.chat.agents.unified_agent import UnifiedAgent


def _make_agent() -> UnifiedAgent:
    return UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
    )


class TestWorkflowStructure:
    """The unified agent should have the XML-section workflow with tool selection,
    dialect rules, agentic workflow, and output instructions."""

    def test_has_tool_selection_section(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "<tool_selection>" in prompt
        assert "</tool_selection>" in prompt

    def test_has_suiteql_dialect_rules(self):
        """Phase 2 (PR A): SuiteQL dialect rules moved from base prompt to
        netsuite.yaml's prompt_fragment. The base agent.system_prompt keeps
        only the cross-reference pointer; the wrapped block lives in the
        profile and is injected per-turn when NetSuite tools are present.
        """
        from app.services.chat.knowledge_profiles.loader import load_all_profiles

        agent = _make_agent()
        prompt = agent.system_prompt
        # Cross-reference must remain so tool_selection's "Follow ALL ..." pointer resolves
        assert "<suiteql_dialect_rules>" in prompt
        # Full wrapped block lives on the netsuite profile
        netsuite = next(
            (p for p in load_all_profiles() if p.profile_id == "netsuite"),
            None,
        )
        assert netsuite is not None, "netsuite.yaml profile did not load"
        assert "<suiteql_dialect_rules>" in netsuite.prompt_fragment
        assert "</suiteql_dialect_rules>" in netsuite.prompt_fragment

    def test_has_agentic_workflow(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "<agentic_workflow>" in prompt
        assert "</agentic_workflow>" in prompt

    def test_has_output_instructions(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "<output_instructions>" in prompt
        assert "</output_instructions>" in prompt

    def test_has_custom_records_guidance(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "CUSTOM RECORD" in prompt or "customrecord_" in prompt

    def test_has_check_context_first(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "CHECK CONTEXT FIRST" in prompt or "tenant_vernacular" in prompt

    def test_has_preflight_schema_check(self):
        """Phase 2 (PR A): PREFLIGHT SCHEMA CHECK moved from base prompt into
        netsuite.yaml's prompt_fragment (part of the suiteql_dialect_rules
        block). The rule is injected per-turn when NetSuite tools are present.
        """
        from app.services.chat.knowledge_profiles.loader import load_all_profiles

        netsuite = next(
            (p for p in load_all_profiles() if p.profile_id == "netsuite"),
            None,
        )
        assert netsuite is not None, "netsuite.yaml profile did not load"
        assert "PREFLIGHT SCHEMA CHECK" in netsuite.prompt_fragment

    def test_has_execute_one_query(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "EXECUTE ONE QUERY" in prompt

    def test_has_error_recovery(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "ERROR RECOVERY" in prompt

    def test_has_stop_when_done(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STOP WHEN YOU HAVE DATA" in prompt

    def test_old_decision_order_removed(self):
        """The old 5-step DECISION ORDER should no longer exist."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "DECISION ORDER (follow this, nothing else)" not in prompt

    def test_budget_stated(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "BUDGET" in prompt
        assert "tool call" in prompt


class TestAntiEnrichmentRules:
    """The unified agent should have explicit anti-enrichment rules in the agentic workflow."""

    def test_anti_enrichment_in_agentic_workflow(self):
        """Anti-enrichment rules must be inside <agentic_workflow>, not at the bottom."""
        agent = _make_agent()
        prompt = agent.system_prompt
        workflow_start = prompt.index("<agentic_workflow>")
        workflow_end = prompt.index("</agentic_workflow>")
        anti_enrichment_pos = prompt.index("ANTI-ENRICHMENT")
        # Anti-enrichment must be between agentic_workflow tags
        assert workflow_start < anti_enrichment_pos < workflow_end

    def test_rma_anti_enrichment(self):
        """Should NOT join item receipts to 'prove' receipt status."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "Do NOT join item receipts" in prompt or "NOT join item receipts" in prompt

    def test_general_anti_enrichment_rule(self):
        """General rule: if status filter answers the question, stop."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "No cross-reference joins" in prompt or "No cross-reference" in prompt or "status codes answer" in prompt


class TestRMAStatusCodes:
    """RMA status codes must match the golden dataset.

    Phase 2 (PR A): the detailed status-code mappings (D=..., E=..., F=...)
    moved from the unified agent's base prompt into netsuite.yaml's
    prompt_fragment. They're injected per-turn when NetSuite tools are
    present. The anti-enrichment IN-clause example still lives in the
    base prompt (it's generic workflow guidance, not dialect-specific).
    """

    def _netsuite_fragment(self) -> str:
        from app.services.chat.knowledge_profiles.loader import load_all_profiles

        netsuite = next(
            (p for p in load_all_profiles() if p.profile_id == "netsuite"),
            None,
        )
        assert netsuite is not None, "netsuite.yaml profile did not load"
        return netsuite.prompt_fragment

    def test_rma_d_partially_received(self):
        fragment = self._netsuite_fragment()
        assert "D=Partially Received" in fragment

    def test_rma_e_received(self):
        """E should be 'Received', not 'Pending Refund/Partially Received'."""
        fragment = self._netsuite_fragment()
        # The golden dataset says E=Received
        assert "E=Received" in fragment
        # The old wrong value should be gone
        assert "E=Pending Refund/Partially Received" not in fragment

    def test_rma_f_closed(self):
        """F should be 'Closed', not 'Pending Refund'."""
        fragment = self._netsuite_fragment()
        # The golden dataset says F=Closed
        assert "F=Closed" in fragment

    def test_rma_received_filter(self):
        """'Received' RMAs should use status IN ('D', 'E', 'F', 'G', 'H').

        The anti-enrichment example lives in the base agentic_workflow block
        (it's generic workflow guidance, not dialect-specific), so it stays
        in the unified agent's system_prompt even after Phase 2.
        """
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "IN ('D', 'E', 'F', 'G', 'H')" in prompt or "IN ('D','E','F','G','H')" in prompt


class TestPromptSyncWithSuiteQLAgent:
    """Critical rules must exist in BOTH unified and SuiteQL agent prompts."""

    def test_both_have_preflight_schema_check(self):
        """Phase 2 (PR A): the unified agent no longer carries PREFLIGHT
        SCHEMA CHECK in its base prompt — it was moved to netsuite.yaml's
        prompt_fragment for per-turn injection. The SuiteQL agent still
        carries it inline. Sync now means: unified reaches the rule via
        the profile, SuiteQL has it inline; both paths expose the rule.
        """
        from app.services.chat.agents.suiteql_agent import SuiteQLAgent
        from app.services.chat.knowledge_profiles.loader import load_all_profiles

        suiteql = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        ).system_prompt
        netsuite_profile = next(
            (p for p in load_all_profiles() if p.profile_id == "netsuite"),
            None,
        )
        assert netsuite_profile is not None, "netsuite.yaml profile did not load"

        # Unified reaches the rule via the injected profile fragment
        assert "PREFLIGHT SCHEMA CHECK" in netsuite_profile.prompt_fragment
        # SuiteQL agent still carries the rule inline
        assert "PREFLIGHT SCHEMA CHECK" in suiteql

    def test_both_have_stop_when_done(self):
        from app.services.chat.agents.suiteql_agent import SuiteQLAgent

        unified = _make_agent().system_prompt
        suiteql = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        ).system_prompt

        assert "STOP WHEN YOU HAVE DATA" in unified
        assert "STOP WHEN YOU HAVE DATA" in suiteql

    def test_both_have_mandatory_execution_rule(self):
        from app.services.chat.agents.suiteql_agent import SuiteQLAgent

        unified = _make_agent().system_prompt
        suiteql = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        ).system_prompt

        assert "DATA FRESHNESS RULES" in unified
        assert "DATA FRESHNESS RULES" in suiteql


class TestInvestigationMode:
    """Investigation mode should have different prompt and higher step budget."""

    def test_max_steps_investigation(self):
        agent = _make_agent()
        agent._context_need = "full"
        assert agent.max_steps == 40

    def test_max_steps_data(self):
        agent = _make_agent()
        agent._context_need = "data"
        assert agent.max_steps == 12

    def test_max_steps_default(self):
        agent = _make_agent()
        assert agent.max_steps == 12

    def test_investigation_prompt_has_progressive_output(self):
        agent = _make_agent()
        agent._context_need = "full"
        prompt = agent.system_prompt
        assert "progressively" in prompt
        assert "chronological narrative" in prompt

    def test_investigation_prompt_no_one_sentence(self):
        agent = _make_agent()
        agent._context_need = "full"
        prompt = agent.system_prompt
        assert "ONLY ONE sentence" not in prompt

    def test_data_prompt_keeps_one_sentence(self):
        agent = _make_agent()
        agent._context_need = "data"
        prompt = agent.system_prompt
        assert "ONE sentence summary" in prompt

    def test_investigation_has_systemnote_expertise(self):
        agent = _make_agent()
        agent._context_need = "full"
        prompt = agent.system_prompt
        assert "systemnote_expertise" in prompt
        assert "recordtypeid = -30" in prompt

    def test_data_no_systemnote_expertise(self):
        agent = _make_agent()
        agent._context_need = "data"
        prompt = agent.system_prompt
        assert "systemnote_expertise" not in prompt

    def test_early_exit_guard_exists_in_base_agent(self):
        """base_agent.py must check _context_need before early exit."""
        import inspect

        from app.services.chat.agents.base_agent import BaseSpecialistAgent

        source = inspect.getsource(BaseSpecialistAgent.run_streaming)
        # The early exit block must include the context_need guard (uses getattr for safety)
        assert "_context_need" in source and '!= "full"' in source

    def test_nudge_guard_exists_in_base_agent(self):
        """base_agent.py must check _context_need before nudging to stop."""
        import inspect

        from app.services.chat.agents.base_agent import BaseSpecialistAgent

        source = inspect.getsource(BaseSpecialistAgent.run_streaming)
        # Count occurrences — should appear twice (early exit + nudge)
        count = source.count("_context_need")
        assert count >= 2, f"Expected at least 2 context_need references, found {count}"


class TestCurrentDateInjection:
    """The unified agent must ALWAYS inject today's date into the system prompt,
    even when the client does not provide a timezone header. Before this fix,
    date injection was gated on `self._user_timezone` — clients that didn't send
    X-Timezone (e.g. background MCP consumers, pre-fix frontend) left the LLM
    with no current-date anchor and it would guess from training cutoff,
    producing year-stale results like 'March 2025' for 'last 4 months' queries.
    """

    def _agent_with_timezone(self, tz: str | None) -> UnifiedAgent:
        agent = UnifiedAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        # _user_timezone is normally populated by _setup_context from the turn
        # context; set it directly so we can test system_prompt in isolation.
        agent._user_timezone = tz
        return agent

    def test_injects_current_date_without_timezone(self):
        agent = self._agent_with_timezone(None)
        prompt = agent.system_prompt
        assert "## CURRENT DATE & TIME" in prompt
        assert "Timezone: UTC" in prompt
        assert "Today:" in prompt

    def test_injects_local_date_with_timezone(self):
        agent = self._agent_with_timezone("America/Los_Angeles")
        prompt = agent.system_prompt
        assert "## CURRENT DATE & TIME" in prompt
        assert "Timezone: America/Los_Angeles" in prompt

    def test_date_block_contains_iso_today(self):
        """Prompt must contain today's literal YYYY-MM-DD so the LLM can't misread it."""
        from datetime import datetime, timezone

        agent = self._agent_with_timezone(None)
        prompt = agent.system_prompt
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today_iso in prompt, f"Expected {today_iso} in prompt"

    def test_date_block_includes_last_n_months_anchoring_hint(self):
        """The date block should tell the LLM how to anchor 'last N months' queries
        so it doesn't use the current partial month as the endpoint."""
        agent = self._agent_with_timezone(None)
        prompt = agent.system_prompt
        assert "last N months" in prompt
        assert "anchor" in prompt.lower()

    def test_date_injection_survives_invalid_timezone(self):
        """Invalid timezone strings should NOT crash the prompt build —
        falls back to UTC."""
        agent = self._agent_with_timezone("Not/A/Real/Timezone")
        prompt = agent.system_prompt
        # Should still have the current date block, with UTC fallback
        assert "## CURRENT DATE & TIME" in prompt
        assert "Timezone: UTC" in prompt
