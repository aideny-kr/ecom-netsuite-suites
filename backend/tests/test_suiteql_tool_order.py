"""NetSuite SuiteQL tool order: local first, MCP for standard tables, try the other before giving up.

Benchmark on staging, 2026-09-29, 18 vs-MCP sales cases:
- Sonnet 5.5 followed "ns_runCustomSuiteQL (MCP, preferred)" literally. It queried the external MCP
  tool, which sees only standard tables, found nothing for Framework's custom shipping-country
  fields, and gave up. Accuracy was 0.79 at med; the expected local tool was used in 1-2 of 18 cases.
- Sonnet 5 had ignored that preference and used the local tool (0.89; 16/18).
Both tools run on tenant-level credentials, so the order changes which tool is tried first, not
what a user can see.

The rule is one constant (tool_guidance.SUITEQL_TOOL_ORDER), embedded in every live prompt. Review
round 1 of #355 found why: hand-copied variants drifted, one live copy was missed
(AGENTIC_SYSTEM_PROMPT), and phrase-matching tests passed a negated sentence.
"""

import re
from functools import cache
from uuid import uuid4

import pytest

from app.services.chat.tool_guidance import SUITEQL_TOOL_ORDER

MCP_FIRST = re.compile(
    r"MCP, preferred|\(preferred\)|prefer external MCP|local,? fallback|prefer over local|"
    r"prefer these for execution|MCP SuiteQL tool if available|otherwise fall back to netsuite_suiteql",
    re.IGNORECASE,
)


def _unified_prompt(context_need):
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id="t", context_need=context_need)
    return agent.system_prompt


@cache
def _live_prompts():
    """Every prompt a chat turn can actually receive that states the SuiteQL tool order."""
    from app.services import prompt_template_service
    from app.services.chat import prompts, tool_inventory

    tools = [
        {"name": "netsuite_suiteql"},
        {"name": "ext__abc__ns_runCustomSuiteQL"},
        {"name": "ext__abc__ns_runReport"},
    ]
    return {
        "unified agent (FULL)": _unified_prompt("FULL"),
        "unified agent (full, slimmed tool guidance)": _unified_prompt("full"),
        "tool inventory (MCP entry + execution priority)": tool_inventory.build_mcp_execution_guidance(tools),
        "prompt template tool rules": prompt_template_service._build_tool_rules_section(),
        # get_active_template() returns this verbatim for any tenant without a custom template.
        "agentic system prompt (template fallback)": prompts.AGENTIC_SYSTEM_PROMPT,
    }


LIVE = [
    "unified agent (FULL)",
    "unified agent (full, slimmed tool guidance)",
    "tool inventory (MCP entry + execution priority)",
    "prompt template tool rules",
    "agentic system prompt (template fallback)",
]


def test_the_rule_says_local_first_and_try_the_other_tool():
    assert "local netsuite_suiteql tool first" in SUITEQL_TOOL_ORDER
    assert "try the other tool before concluding the data does not exist" in SUITEQL_TOOL_ORDER


@pytest.mark.parametrize("name", LIVE)
def test_every_live_prompt_carries_the_one_tool_order_rule(name):
    assert SUITEQL_TOOL_ORDER in _live_prompts()[name], name


@pytest.mark.parametrize("name", LIVE)
def test_no_live_prompt_keeps_an_mcp_first_wording(name):
    found = MCP_FIRST.search(_live_prompts()[name])
    assert not found, f"{name}: {found.group(0)!r}"


def test_the_legacy_router_prompt_keeps_no_mcp_first_wording():
    from app.services.chat.prompts import ROUTER_PROMPT

    assert not MCP_FIRST.search(ROUTER_PROMPT)
