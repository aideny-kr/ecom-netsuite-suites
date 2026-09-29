"""Tool-choice guidance shared by every chat prompt, so the prompts cannot drift apart.

The SuiteQL order was hand-copied into eight prompts, each worded differently. They told the
agent to prefer the external MCP tool, which sees only standard tables. Sonnet 5.5 obeys that
literally and stopped finding Framework's custom fields (benchmark 2026-09-29). The retry is
limited to standard tables because an MCP retry of a custom-record query is empty by construction,
and reporting it as a second empty result would state a guess as a finding (#355 review round 3).
Every prompt
that states the order embeds this constant; tests/test_suiteql_tool_order.py enforces it.
"""

SUITEQL_TOOL_ORDER = (
    "For NetSuite queries, use the local netsuite_suiteql tool first: it sees custom records and custom "
    "fields. Use the external MCP SuiteQL tool (ns_runCustomSuiteQL) for standard tables or when the local "
    "tool is unavailable. If a standard-table query returns zero rows or an error, try the other tool once "
    "before concluding the data does not exist. Never retry a custom record or custom field query on the MCP "
    "tool: it cannot see them, so its empty result proves nothing. Fix the local query instead."
)
