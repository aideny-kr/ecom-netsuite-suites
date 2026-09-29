"""NetSuite SuiteQL tool order: local first, MCP for standard tables, try the other before giving up.

Benchmark on staging, 2026-09-29, 18 vs-MCP sales cases:
- Sonnet 5.5 followed "ns_runCustomSuiteQL (MCP, preferred)" literally. It queried the external MCP
  tool, which sees only standard tables, found nothing for Framework's custom shipping-country
  fields, and gave up. Accuracy was 0.79 at med; the expected local tool was used in 1-2 of 18 cases.
- Sonnet 5 had ignored that preference and used the local tool (0.89; 16/18).
Both tools run on tenant-level credentials, so the order changes which tool is tried first, not
what a user can see.
"""

import re
from uuid import uuid4

import pytest

MCP_FIRST = re.compile(
    r"MCP, preferred|\(preferred\)|prefer external MCP|local,? fallback|prefer over local|prefer these for execution",
    re.IGNORECASE,
)
LOCAL_FIRST = re.compile(r"netsuite_suiteql[^.\n]{0,70}\bfirst\b", re.IGNORECASE)
TRY_THE_OTHER = re.compile(r"zero rows.{0,80}(other|netsuite_suiteql|ns_runCustomSuiteQL)", re.IGNORECASE | re.DOTALL)


def _unified_prompt(context_need):
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id="t", context_need=context_need)
    return agent.system_prompt


def _prompts():
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
        "tool inventory execution priority": tool_inventory.build_mcp_execution_guidance(tools),
        "prompt template tool rules": prompt_template_service._build_tool_rules_section(),
        "router prompt": prompts.ROUTER_PROMPT,
    }


@pytest.mark.parametrize("name", list(_prompts()))
def test_no_prompt_tells_the_agent_to_prefer_the_mcp_query_tool(name):
    text = _prompts()[name]
    assert not MCP_FIRST.search(text), f"{name}: {MCP_FIRST.search(text).group(0)!r}"


@pytest.mark.parametrize(
    "name",
    [
        "unified agent (FULL)",
        "unified agent (full, slimmed tool guidance)",
        "tool inventory execution priority",
        "prompt template tool rules",
    ],
)
def test_the_agent_is_told_to_try_the_other_tool_before_concluding_there_is_no_data(name):
    text = _prompts()[name]
    assert TRY_THE_OTHER.search(text), name
    assert LOCAL_FIRST.search(text), name
