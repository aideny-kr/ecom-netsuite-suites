"""The unified agent's 'MUST use these script IDs / OVERRIDE' directive must be
scoped to the <resolved_entities> block only — NOT the whole vernacular. An
<ambiguous_entities> block (list values / non-column references) must be framed
as advisory so the agent does not blindly filter on a list-value id.
"""

import uuid

from app.services.chat.agents.unified_agent import UnifiedAgent


def _agent_with_vernacular() -> UnifiedAgent:
    agent = UnifiedAgent(uuid.uuid4(), uuid.uuid4(), "corr-test")
    agent._tenant_vernacular = (
        "<tenant_vernacular>\n"
        "  <resolved_entities><entity>"
        "<internal_script_id>custitem_fw_platform</internal_script_id>"
        "</entity></resolved_entities>\n"
        "  <ambiguous_entities><ambiguous_term>"
        "<matched_value>customlist_fw_cpu_platform.14</matched_value>"
        "</ambiguous_term></ambiguous_entities>\n"
        "</tenant_vernacular>"
    )
    return agent


def test_must_use_authority_scoped_to_resolved_entities():
    prompt = _agent_with_vernacular().system_prompt

    # Authority is scoped to the resolved_entities block...
    assert "entities in the <resolved_entities> block below have been pre-resolved" in prompt
    # ...and the old blanket wording is gone.
    assert "The entities below have been pre-resolved" not in prompt


def test_ambiguous_entities_framed_as_advisory():
    prompt = _agent_with_vernacular().system_prompt

    assert "<ambiguous_entities> block are ADVISORY ONLY" in prompt
