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

# Read tools: the enumerated real Celigo read catalog, matched EXACTLY -- not a
# `list_` prefix rule. A prefix rule looked equivalent when every known read
# tool happened to start with `list_`, but the tool NAME here is reported by
# whatever server answers for a given celigo_mcp connector's server_url. A
# prefix check trusts that name; enumeration does not, so a remote tool named
# e.g. `list_delete_everything` is denied like any other unrecognised name.
_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_integrations",
        "list_flows",
        "list_exports",
        "list_imports",
        "list_connections",
        "list_scripts",
        "list_flow_errors",
        "list_jobs",
        "list_current_jobs",
        "list_execution_logs",
        "list_audit_log_entries",
        "list_tags",
        "list_users",
        "list_environments",
        "list_apis",
        "list_iclients",
        "list_file_definitions",
        "list_lookup_caches",
        "list_lookup_cache_data",
        "list_edi_profiles",
        "list_edi_transactions",
        "list_storage_items",
        "list_marketplace",
        "list_mcp_servers",
        "list_tools",
        "list_ai_agents",
        "list_guardrails",
        "get_schema",
        "search_knowledge_base",
    }
)

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

    Fails closed: an unrecognised name is denied, including one that merely
    LOOKS like a read tool (e.g. `list_delete_everything`). Matching is exact
    against `_READ_TOOLS`, not a `list_` prefix check -- enumerated write tools
    are checked first and are never readable either way.
    """
    if not raw_tool_name:
        return False
    if raw_tool_name in CELIGO_WRITE_VERBS:
        return False
    return raw_tool_name in _READ_TOOLS
