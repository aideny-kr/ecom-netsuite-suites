"""UnifiedAgent._write_repair_directive — mirror of the existing, proven
_plan_mode_resume_directive injection pattern (agentic-repair design
requirement B). The orchestrator sets this attribute per-turn on a re-entry
after NetSuite rejects an approved write; the agent's system_prompt property
must surface it to the model so the repair turn is actually scoped.
"""

from __future__ import annotations

import uuid

from app.services.chat.agents.unified_agent import UnifiedAgent


def _make_agent() -> UnifiedAgent:
    return UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="test")


class TestWriteRepairDirectiveDefault:
    def test_defaults_to_empty_string(self):
        agent = _make_agent()
        assert agent._write_repair_directive == ""

    def test_empty_directive_does_not_appear_in_prompt(self):
        agent = _make_agent()
        prompt = agent.system_prompt
        assert "NetSuite rejected" not in prompt


class TestWriteRepairDirectiveInjection:
    def test_directive_appears_in_system_prompt_when_set(self):
        agent = _make_agent()
        agent._write_repair_directive = "NetSuite rejected the previous attempt: missing subsidiary."
        prompt = agent.system_prompt
        assert "NetSuite rejected the previous attempt: missing subsidiary." in prompt

    def test_directive_appears_after_plan_mode_resume_directive(self):
        """Mirrors the existing augmentation-then-resume-directive ordering
        rule (later injections override earlier framing) — the write-repair
        directive is the most specific, most recent instruction, so it must
        render last."""
        agent = _make_agent()
        agent._plan_mode_augmentation = "PLAN MODE AUGMENTATION MARKER"
        agent._plan_mode_resume_directive = "PLAN MODE RESUME MARKER"
        agent._write_repair_directive = "WRITE REPAIR MARKER"
        prompt = agent.system_prompt

        aug_idx = prompt.index("PLAN MODE AUGMENTATION MARKER")
        resume_idx = prompt.index("PLAN MODE RESUME MARKER")
        repair_idx = prompt.index("WRITE REPAIR MARKER")
        assert aug_idx < resume_idx < repair_idx
