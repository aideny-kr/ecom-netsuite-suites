"""Tests for unified agent 7-step workflow (Fix 4 — 10x Agent Quality).

Validates that the unified agent prompt has the battle-tested workflow from
the SuiteQL agent, anti-enrichment rules, and correct RMA status codes.
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
    """The unified agent should have the 7-step workflow, not the old 5-step DECISION ORDER."""

    def test_has_step_0_custom_records(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 0" in prompt
        assert "CUSTOM RECORDS FIRST" in prompt

    def test_has_step_1_check_context(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 1" in prompt

    def test_has_step_2_domain_knowledge(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 2" in prompt
        assert "domain_knowledge" in prompt.lower()

    def test_has_step_3_preflight_schema(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 3" in prompt
        assert "PREFLIGHT" in prompt

    def test_has_step_4_execute_one_query(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 4" in prompt
        assert "EXECUTE" in prompt

    def test_has_step_5_error_recovery(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 5" in prompt
        assert "ERROR RECOVERY" in prompt or "Error" in prompt

    def test_has_step_6_stop_when_done(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 6" in prompt
        assert "STOP" in prompt

    def test_has_step_7_documentation(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "STEP 7" in prompt

    def test_old_decision_order_removed(self):
        """The old 5-step DECISION ORDER should no longer exist."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "DECISION ORDER (follow this, nothing else)" not in prompt

    def test_budget_stated(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "BUDGET" in prompt
        assert "tool calls" in prompt or "tool call" in prompt


class TestAntiEnrichmentRules:
    """The unified agent should have explicit anti-enrichment rules in STEP 4."""

    def test_anti_enrichment_in_step_4(self):
        """Anti-enrichment rules must be inside STEP 4, not at the bottom."""
        agent = _make_agent()
        prompt = agent.system_prompt
        step_4_pos = prompt.index("STEP 4")
        step_5_pos = prompt.index("STEP 5")
        anti_enrichment_pos = prompt.index("ANTI-ENRICHMENT")
        # Anti-enrichment must be between STEP 4 and STEP 5
        assert step_4_pos < anti_enrichment_pos < step_5_pos

    def test_rma_anti_enrichment(self):
        """Should NOT join item receipts to 'prove' receipt status."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "Do NOT join item receipts" in prompt or "NOT join item receipts" in prompt

    def test_general_anti_enrichment_rule(self):
        """General rule: if status filter answers the question, stop."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "No cross-reference joins" in prompt or "No cross-reference" in prompt


class TestRMAStatusCodes:
    """RMA status codes must match the golden dataset."""

    def test_rma_d_partially_received(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "D=Partially Received" in prompt

    def test_rma_e_received(self):
        """E should be 'Received', not 'Pending Refund/Partially Received'."""
        agent = _make_agent()
        prompt = agent.system_prompt
        # The golden dataset says E=Received
        assert "E=Received" in prompt
        # The old wrong value should be gone
        assert "E=Pending Refund/Partially Received" not in prompt

    def test_rma_f_closed(self):
        """F should be 'Closed', not 'Pending Refund'."""
        agent = _make_agent()
        prompt = agent.system_prompt
        # The golden dataset says F=Closed
        assert "F=Closed" in prompt

    def test_rma_received_filter(self):
        """'Received' RMAs should use status IN ('D', 'E', 'F', 'G', 'H')."""
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "IN ('D', 'E', 'F', 'G', 'H')" in prompt or "IN ('D','E','F','G','H')" in prompt


class TestPromptSyncWithSuiteQLAgent:
    """Critical rules must exist in BOTH unified and SuiteQL agent prompts."""

    def test_both_have_preflight_schema_check(self):
        from app.services.chat.agents.suiteql_agent import SuiteQLAgent

        unified = _make_agent().system_prompt
        suiteql = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        ).system_prompt

        assert "PREFLIGHT SCHEMA CHECK" in unified
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

        assert "MANDATORY EXECUTION RULE" in unified
        assert "MANDATORY EXECUTION RULE" in suiteql


class TestInvestigationMode:
    """Investigation mode should have different prompt and higher step budget."""

    def test_max_steps_investigation(self):
        agent = _make_agent()
        agent._context_need = "full"
        assert agent.max_steps == 12

    def test_max_steps_data(self):
        agent = _make_agent()
        agent._context_need = "data"
        assert agent.max_steps == 6

    def test_max_steps_default(self):
        agent = _make_agent()
        assert agent.max_steps == 6

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
        assert "ONLY ONE sentence" in prompt

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
        assert '_context_need' in source and '!= "full"' in source

    def test_nudge_guard_exists_in_base_agent(self):
        """base_agent.py must check _context_need before nudging to stop."""
        import inspect
        from app.services.chat.agents.base_agent import BaseSpecialistAgent

        source = inspect.getsource(BaseSpecialistAgent.run_streaming)
        # Count occurrences — should appear twice (early exit + nudge)
        count = source.count('_context_need')
        assert count >= 2, f"Expected at least 2 context_need references, found {count}"
