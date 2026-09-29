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


LIVE = list(_live_prompts())  # derived, so a prompt added above cannot be skipped (#355 round 2)


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


def test_the_rule_is_stated_once_in_the_tool_inventory():
    # #355 round 2: it was emitted twice in one guidance block, once per section.
    assert _live_prompts()["tool inventory (MCP entry + execution priority)"].count(SUITEQL_TOOL_ORDER) == 1


def test_the_error_recovery_rules_do_not_stop_at_zero_rows_before_trying_the_other_tool():
    # #355 round 2: "0 rows on other tables -> report '0 rows found'" contradicted the rule in the
    # same prompt, the give-up behaviour this change exists to remove.
    prompt = _live_prompts()["unified agent (FULL)"]
    zero_row_lines = [line for line in prompt.splitlines() if line.startswith("- 0 rows on other tables")]
    assert zero_row_lines and all("other SuiteQL tool" in line for line in zero_row_lines)


async def test_a_saved_template_serves_the_current_tool_rules():
    # #355 round 2: get_active_template() returned a tenant's stored text verbatim, so templates
    # saved before this change (Framework's, 2026-03-16) kept "prefer external MCP tools".
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.services import prompt_template_service as pts

    old_rules = (
        "WORKFLOW GUIDANCE:\n- To query NetSuite data, prefer external MCP tools (prefixed with 'ext__') if "
        "available. These connect directly to NetSuite and are the most reliable option.\n- If no external "
        "MCP tools are available, use the netsuite_suiteql tool as fallback."
    )
    saved = SimpleNamespace(
        template_text=f"IDENTITY\n\n{old_rules}\n\nRESPONSE RULES", sections={"tool_rules": old_rules}
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = saved
    db = AsyncMock()
    db.execute.return_value = result
    served = await pts.get_active_template(db, "tenant")
    assert SUITEQL_TOOL_ORDER in served and not MCP_FIRST.search(served)
    assert served.startswith("IDENTITY\n\n") and served.endswith("\n\nRESPONSE RULES")
