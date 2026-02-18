"""Deterministic system prompt template generation from structured tenant profile data.

No LLM calls — pure string formatting from structured metadata.
"""

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.policy_profile import PolicyProfile
from app.models.prompt_template import SystemPromptTemplate
from app.models.tenant_profile import TenantProfile
from app.services.chat.prompts import AGENTIC_SYSTEM_PROMPT

logger = structlog.get_logger()


def _build_identity_section(profile: TenantProfile) -> str:
    parts = [
        "You are a helpful data assistant for an e-commerce operations platform "
        "connected to NetSuite via MCP (Model Context Protocol)."
    ]
    if profile.industry:
        parts.append(f"The business operates in the {profile.industry} industry.")
    if profile.business_description:
        parts.append(f"Business context: {profile.business_description}")
    if getattr(profile, "team_size", None):
        parts.append(f"Team size: {profile.team_size}")
    return "\n".join(parts)


def _build_netsuite_context_section(profile: TenantProfile) -> str:
    parts = []
    if profile.netsuite_account_id:
        parts.append(f"NetSuite Account ID: {profile.netsuite_account_id}")
    if profile.subsidiaries:
        subs = profile.subsidiaries
        if isinstance(subs, list):
            sub_names = ", ".join(str(s.get("name", s)) if isinstance(s, dict) else str(s) for s in subs)
            parts.append(f"Subsidiaries: {sub_names}")
        elif isinstance(subs, dict):
            parts.append(f"Subsidiaries: {subs}")
    if profile.chart_of_accounts:
        coa = profile.chart_of_accounts
        if isinstance(coa, list) and len(coa) > 0:
            parts.append(f"Chart of Accounts ({len(coa)} accounts available):")
            for acct in coa[:20]:  # Show first 20
                if isinstance(acct, dict):
                    parts.append(f"  - {acct.get('number', '?')}: {acct.get('name', '?')}")
    if profile.item_types:
        items = profile.item_types
        if isinstance(items, list):
            parts.append(f"Item types in use: {', '.join(str(i) for i in items)}")
    if profile.custom_segments:
        parts.append(f"Custom segments: {profile.custom_segments}")
    if profile.fiscal_calendar:
        parts.append(f"Fiscal calendar: {profile.fiscal_calendar}")
    return "\n".join(parts) if parts else ""


def _build_suiteql_rules_section(profile: TenantProfile) -> str:
    parts = [
        "SUITEQL SYNTAX RULES (Oracle-style SQL):",
        "- Use ROWNUM for limiting results: WHERE ROWNUM <= 10 (NOT LIMIT)",
        "- Use NVL() instead of IFNULL() or COALESCE()",
        "- NO Common Table Expressions (CTEs / WITH clauses) — use subqueries instead",
        "- String literals use single quotes: 'value'",
        "- Date filtering: TO_DATE('2024-01-01', 'YYYY-MM-DD')",
        "- Common tables: transaction, transactionline, customer, item, vendor, account, subsidiary",
    ]
    if profile.suiteql_naming:
        naming = profile.suiteql_naming
        if isinstance(naming, dict):
            for key, value in naming.items():
                parts.append(f"- {key}: {value}")
    return "\n".join(parts)


def _build_tool_rules_section() -> str:
    return (
        "WORKFLOW GUIDANCE:\n"
        "- When the user asks about NetSuite data, call ns_getSuiteQLMetadata FIRST "
        "to discover available field names, THEN construct a SuiteQL query.\n"
        "- If a query fails with 'Unknown identifier', look up correct field names "
        "using metadata tools, fix the query, and retry automatically.\n"
        "- You may call tools multiple times in sequence."
    )


def _build_policy_constraints_section(policy: PolicyProfile | None) -> str:
    if not policy:
        return "You have read-only access to data. Do not modify any records."

    parts = ["POLICY CONSTRAINTS:"]
    if getattr(policy, "sensitivity_default", None):
        parts.append(f"- Sensitivity default: {policy.sensitivity_default}")
    if policy.read_only_mode:
        parts.append("- READ-ONLY MODE: You must not modify, create, or delete any records.")
    if policy.allowed_record_types:
        types = policy.allowed_record_types
        if isinstance(types, list):
            parts.append(f"- Allowed record types: {', '.join(types)}")
    if policy.blocked_fields:
        fields = policy.blocked_fields
        if isinstance(fields, list):
            parts.append(f"- Blocked fields (never include in queries): {', '.join(fields)}")
    if getattr(policy, "tool_allowlist", None):
        allowlist = policy.tool_allowlist
        if isinstance(allowlist, list) and allowlist:
            parts.append(f"- Tool allowlist: {', '.join(allowlist)}")
    if policy.max_rows_per_query:
        parts.append(f"- Maximum rows per query: {policy.max_rows_per_query}")
    if policy.require_row_limit:
        parts.append("- Always include a row limit in queries.")
    if policy.custom_rules:
        rules = policy.custom_rules
        if isinstance(rules, list):
            for rule in rules:
                parts.append(f"- {rule}")
    return "\n".join(parts)


def _build_response_rules_section() -> str:
    return (
        "RESPONSE RULES:\n"
        "- Present data in clear, formatted tables when appropriate.\n"
        "- Include relevant context about what the data represents.\n"
        "- If you encounter errors, explain what went wrong and suggest alternatives.\n"
        "- Never fabricate data — only present information from tool results."
    )


def generate_template(
    profile: TenantProfile,
    policy: PolicyProfile | None = None,
) -> tuple[str, dict]:
    """Generate a deterministic system prompt template from structured profile data.

    Returns (template_text, sections_dict).
    """
    sections = {
        "identity": _build_identity_section(profile),
        "netsuite_context": _build_netsuite_context_section(profile),
        "suiteql_rules": _build_suiteql_rules_section(profile),
        "tool_rules": _build_tool_rules_section(),
        "policy_constraints": _build_policy_constraints_section(policy),
        "response_rules": _build_response_rules_section(),
    }

    # Combine non-empty sections
    parts = []
    for key in ["identity", "netsuite_context", "suiteql_rules", "tool_rules", "policy_constraints", "response_rules"]:
        text = sections[key]
        if text:
            parts.append(text)

    template_text = "\n\n".join(parts)
    return template_text, sections


async def generate_and_save_template(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    profile: TenantProfile,
) -> SystemPromptTemplate:
    """Generate template and persist it, deactivating any previous active template."""
    # Get active policy if any
    from app.services.policy_service import get_active_policy

    policy = await get_active_policy(db, tenant_id)

    template_text, sections = generate_template(profile, policy)

    # Deactivate previous active templates
    await db.execute(
        update(SystemPromptTemplate)
        .where(
            SystemPromptTemplate.tenant_id == tenant_id,
            SystemPromptTemplate.is_active.is_(True),
        )
        .values(is_active=False)
    )

    template = SystemPromptTemplate(
        tenant_id=tenant_id,
        version=profile.version,
        profile_id=profile.id,
        policy_id=policy.id if policy else None,
        template_text=template_text,
        sections=sections,
        is_active=True,
        generated_at=datetime.now(timezone.utc),
    )
    db.add(template)
    await db.flush()

    logger.info(
        "prompt_template.generated",
        tenant_id=str(tenant_id),
        template_id=str(template.id),
        profile_version=profile.version,
    )
    return template


async def get_active_template(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> str:
    """Return the active template text for a tenant, or fall back to AGENTIC_SYSTEM_PROMPT."""
    result = await db.execute(
        select(SystemPromptTemplate).where(
            SystemPromptTemplate.tenant_id == tenant_id,
            SystemPromptTemplate.is_active.is_(True),
        )
    )
    template = result.scalar_one_or_none()
    if template:
        return template.template_text
    return AGENTIC_SYSTEM_PROMPT


async def get_active_template_obj(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> SystemPromptTemplate | None:
    """Return the active template object for a tenant."""
    result = await db.execute(
        select(SystemPromptTemplate).where(
            SystemPromptTemplate.tenant_id == tenant_id,
            SystemPromptTemplate.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()
