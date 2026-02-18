"""SuiteQL specialist agent.

Specialises in constructing and executing SuiteQL queries against NetSuite.
Has the tenant's custom field metadata injected into its system prompt so it
knows the exact scriptid for every custom field, record type, subsidiary, etc.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.tools import build_local_tool_definitions

if TYPE_CHECKING:
    from app.models.netsuite_metadata import NetSuiteMetadata
    from app.models.policy_profile import PolicyProfile

# Tools this agent is allowed to use
_SUITEQL_TOOL_NAMES = frozenset(
    {
        "netsuite_suiteql",
        "netsuite_get_metadata",
        "netsuite_connectivity",
    }
)


class SuiteQLAgent(BaseSpecialistAgent):
    """Specialist agent for SuiteQL query construction and execution."""

    def __init__(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
        metadata: NetSuiteMetadata | None = None,
        policy: PolicyProfile | None = None,
    ) -> None:
        super().__init__(tenant_id, user_id, correlation_id)
        self._metadata = metadata
        self._policy = policy
        self._tool_defs: list[dict] | None = None

    @property
    def agent_name(self) -> str:
        return "suiteql"

    @property
    def max_steps(self) -> int:
        return 3  # metadata lookup → query → retry on error

    @property
    def system_prompt(self) -> str:
        parts = [
            "You are a SuiteQL query specialist. Your ONLY job is to construct and execute "
            "accurate SuiteQL queries against NetSuite based on the task given to you.\n",
            "SUITEQL SYNTAX RULES (Oracle-style SQL):",
            "- Use ROWNUM for limiting results: WHERE ROWNUM <= 10 (NOT LIMIT)",
            "- Use NVL() instead of IFNULL() or COALESCE()",
            "- NO Common Table Expressions (CTEs / WITH clauses) — use subqueries instead",
            "- String literals use single quotes: 'value'",
            "- Date filtering: TO_DATE('2024-01-01', 'YYYY-MM-DD')",
            "- Common tables: transaction, transactionline, customer, item, vendor, account, "
            "subsidiary, department, location, employee",
            "- Transaction types: use type field (e.g., type = 'SalesOrd', 'CustInvc', 'VendBill', 'CustPymt')",
            "- Always include a ROWNUM limit to avoid fetching too much data",
            "",
            "ERROR HANDLING:",
            "- If a query fails with 'Unknown identifier', use the netsuite_get_metadata tool "
            "to look up correct field names, then fix and retry the query.",
            "- If a query fails with a syntax error, analyse the error message, fix the query, and retry.",
            "- After retrying, if the query still fails, return the error details clearly.",
            "",
            "OUTPUT FORMAT:",
            "- Return the raw query results as-is. Do NOT interpret or summarise the data — "
            "another agent will handle analysis.",
            "- If the query succeeded, include the SuiteQL query you used.",
        ]

        # Inject custom field metadata
        if self._metadata:
            parts.append("")
            parts.append(self._build_metadata_reference())

        # Inject policy constraints
        if self._policy:
            if self._policy.read_only_mode:
                parts.append("\nYou MUST only execute SELECT queries. No modifications.")
            if self._policy.max_rows_per_query:
                parts.append(f"Maximum rows per query: {self._policy.max_rows_per_query}")
            if self._policy.blocked_fields and isinstance(self._policy.blocked_fields, list):
                parts.append(f"BLOCKED fields (never query these): {', '.join(self._policy.blocked_fields)}")

        return "\n".join(parts)

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            all_tools = build_local_tool_definitions()
            self._tool_defs = [t for t in all_tools if t["name"] in _SUITEQL_TOOL_NAMES]
        return self._tool_defs

    def _build_metadata_reference(self) -> str:
        """Build a concise custom field reference from discovered metadata."""
        md = self._metadata
        if md is None:
            return ""

        max_fields = 40  # Keep it concise for the specialist
        parts = ["CUSTOM FIELDS REFERENCE (from this NetSuite account):"]

        if md.transaction_body_fields and isinstance(md.transaction_body_fields, list):
            parts.append(f"\nTransaction body fields ({len(md.transaction_body_fields)} total):")
            for f in md.transaction_body_fields[:max_fields]:
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}): {f.get('label', '?')}")

        if md.transaction_column_fields and isinstance(md.transaction_column_fields, list):
            parts.append(f"\nTransaction line fields ({len(md.transaction_column_fields)} total):")
            for f in md.transaction_column_fields[:max_fields]:
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}): {f.get('label', '?')}")

        if md.entity_custom_fields and isinstance(md.entity_custom_fields, list):
            parts.append(f"\nEntity custom fields ({len(md.entity_custom_fields)} total):")
            for f in md.entity_custom_fields[:max_fields]:
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}): {f.get('label', '?')}")

        if md.item_custom_fields and isinstance(md.item_custom_fields, list):
            parts.append(f"\nItem custom fields ({len(md.item_custom_fields)} total):")
            for f in md.item_custom_fields[:max_fields]:
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}): {f.get('label', '?')}")

        if md.custom_record_types and isinstance(md.custom_record_types, list):
            parts.append(f"\nCustom record types ({len(md.custom_record_types)} total):")
            for r in md.custom_record_types[:20]:
                parts.append(f"  {r.get('scriptid', '?')}: {r.get('name', '?')}")

        if md.subsidiaries and isinstance(md.subsidiaries, list):
            active = [s for s in md.subsidiaries if s.get("isinactive") != "T"]
            if active:
                parts.append(f"\nSubsidiaries ({len(active)} active):")
                for s in active:
                    parent = f" (parent: {s['parent']})" if s.get("parent") else ""
                    parts.append(f"  ID {s.get('id', '?')}: {s.get('name', '?')}{parent}")

        if md.departments and isinstance(md.departments, list):
            active = [d for d in md.departments if d.get("isinactive") != "T"]
            if active:
                parts.append(f"\nDepartments ({len(active)} active):")
                for d in active[:20]:
                    parts.append(f"  ID {d.get('id', '?')}: {d.get('name', '?')}")

        if md.classifications and isinstance(md.classifications, list):
            active = [c for c in md.classifications if c.get("isinactive") != "T"]
            if active:
                parts.append(f"\nClasses ({len(active)} active):")
                for c in active[:20]:
                    parts.append(f"  ID {c.get('id', '?')}: {c.get('name', '?')}")

        if md.locations and isinstance(md.locations, list):
            active = [loc for loc in md.locations if loc.get("isinactive") != "T"]
            if active:
                parts.append(f"\nLocations ({len(active)} active):")
                for loc in active[:20]:
                    parts.append(f"  ID {loc.get('id', '?')}: {loc.get('name', '?')}")

        return "\n".join(parts)
