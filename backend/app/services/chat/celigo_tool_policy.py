"""Celigo tool policy — the single source of truth for what the agent may call.

Celigo's hosted MCP server ships read tools and write tools in one catalog. This
product exposes Celigo READ-ONLY, so the write tools must be unreachable rather
than merely discouraged: Celigo's own `delete_resource` never blocks server-side,
and prompt instructions are not a control.

Two layers import from this module and must not drift:
  1. tools.build_external_tool_definitions — writes never enter the inventory
  2. tools._execute_external_tool          — the dispatcher refuses them anyway

Layer 2 exists because `.claude/rules/agent-graph.md` #3 names the dispatcher as
the choke point: filtering only at definition time leaves a hole that any new
caller can walk through.

NOTE: the allowlist here is intentionally the OPPOSITE trade-off from
`_BLOCKED_RECORD_TYPES` in mutation_guard.py, which is a deliberate deny-list
(agent-graph.md #1). That surface is open-ended, so an allow-list would fail
closed on every new NetSuite record type. This surface is a small, enumerable,
read-only tool catalog, so failing closed on an unknown name is exactly right.
"""

from __future__ import annotations

# Providers whose tools are governed by this policy.
CELIGO_PROVIDERS: frozenset[str] = frozenset({"celigo_mcp"})

# Read tools: everything Celigo exposes as `list_*`, plus these two explicit reads.
_READ_PREFIX = "list_"
_READ_EXACT: frozenset[str] = frozenset({"get_schema", "search_knowledge_base"})

# Celigo write tools → mutation verb.
#
# Verbs are deliberately coarse: they MUST stay inside
# Literal["create","update","delete","upsert"] (write_confirmation_service.py:39).
# `run_flow` and `deploy_template` are really "execute", but introducing that verb
# would raise a Pydantic ValidationError in the confirmation card. Since this plan
# makes these tools unreachable, the verb only ever renders a card that cannot be
# reached — precision here has no runtime effect. Widen the Literal (and the
# frontend WriteConfirmationCard) first if Celigo writes are ever enabled.
CELIGO_WRITE_VERBS: dict[str, str] = {
    "upsert_connection": "upsert",
    "upsert_export": "upsert",
    "upsert_import": "upsert",
    "upsert_flow": "upsert",
    "upsert_integration": "upsert",
    "upsert_script": "upsert",
    "upsert_api": "upsert",
    "upsert_ai_agent": "upsert",
    "upsert_edi_profile": "upsert",
    "upsert_file_definition": "upsert",
    "upsert_guardrail": "upsert",
    "upsert_iclient": "upsert",
    "upsert_lookup_cache": "upsert",
    "upsert_lookup_cache_data": "upsert",
    "upsert_mcp_server": "upsert",
    "upsert_storage_item": "upsert",
    "upsert_tool": "upsert",
    "patch_flow": "update",
    "update_edi_fa_status": "update",
    "update_flow_error_retry_data": "update",
    "delete_resource": "delete",
    "delete_lookup_cache_data": "delete",
    "run_flow": "update",
    "deploy_template": "update",
    "restore_storage_item": "update",
    "manage_user": "update",
    "cancel_job": "update",
    "cancel_storage_upload": "update",
    "triage_flow_errors": "update",
}


def is_celigo_provider(provider: str) -> bool:
    """Return True if *provider* is governed by the Celigo read-only policy."""
    return provider in CELIGO_PROVIDERS


def is_read_only_celigo_tool(raw_tool_name: str) -> bool:
    """Return True if *raw_tool_name* is a Celigo read tool.

    Fails closed: an unrecognised name is denied. Enumerated write tools are never
    readable, even if a future Celigo release names one `list_something`.
    """
    if not raw_tool_name:
        return False
    if raw_tool_name in CELIGO_WRITE_VERBS:
        return False
    if raw_tool_name in _READ_EXACT:
        return True
    return raw_tool_name.startswith(_READ_PREFIX)
