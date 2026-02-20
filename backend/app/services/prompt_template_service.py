"""Deterministic system prompt template generation from structured tenant profile data.

No LLM calls — pure string formatting from structured metadata.
"""

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.netsuite_metadata import NetSuiteMetadata
from app.models.policy_profile import PolicyProfile
from app.models.prompt_template import SystemPromptTemplate
from app.models.tenant_profile import TenantProfile
from app.services.chat.prompts import AGENTIC_SYSTEM_PROMPT

logger = structlog.get_logger()

# Maximum number of fields to include in the prompt per category
# (keeps token count manageable while covering most customisations)
_MAX_FIELDS_PER_SECTION = 60


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


def _build_netsuite_customizations_section(metadata: NetSuiteMetadata | None) -> str:
    """Build prompt section listing discovered custom fields and org hierarchies.

    This is the key section that gives the AI knowledge of tenant-specific
    customisations so it can write correct SuiteQL queries.
    """
    if metadata is None:
        return ""

    parts: list[str] = ["NETSUITE CUSTOM FIELDS AND ORGANISATION (discovered from your account):"]

    # ── Transaction body fields (custbody_*) ──────────────────────
    if metadata.transaction_body_fields and isinstance(metadata.transaction_body_fields, list):
        fields = metadata.transaction_body_fields[:_MAX_FIELDS_PER_SECTION]
        parts.append(f"\n## Custom Transaction Body Fields ({len(metadata.transaction_body_fields)} total)")
        parts.append("Use these in SELECT/WHERE on the `transaction` table:")
        for f in fields:
            line = f"- `{f.get('scriptid', '?')}` ({f.get('fieldtype', '?')}): {f.get('name', '?')}"
            if f.get("ismandatory") == "T":
                line += " [REQUIRED]"
            if f.get("fieldvaluetype"):
                line += f" — {f['fieldvaluetype']}"
            parts.append(line)

    # ── Transaction column/line fields (custcol_*) ────────────────
    if metadata.transaction_column_fields and isinstance(metadata.transaction_column_fields, list):
        fields = metadata.transaction_column_fields[:_MAX_FIELDS_PER_SECTION]
        parts.append(f"\n## Custom Transaction Line Fields ({len(metadata.transaction_column_fields)} total)")
        parts.append("Use these in SELECT/WHERE on the `transactionline` table:")
        for f in fields:
            line = f"- `{f.get('scriptid', '?')}` ({f.get('fieldtype', '?')}): {f.get('name', '?')}"
            if f.get("fieldvaluetype"):
                line += f" — {f['fieldvaluetype']}"
            parts.append(line)

    # ── Entity custom fields (custentity_*) ───────────────────────
    if metadata.entity_custom_fields and isinstance(metadata.entity_custom_fields, list):
        fields = metadata.entity_custom_fields[:_MAX_FIELDS_PER_SECTION]
        parts.append(f"\n## Custom Entity Fields ({len(metadata.entity_custom_fields)} total)")
        parts.append("Use on `customer`, `vendor`, or `employee` tables:")
        for f in fields:
            vtype = f" (value type: {f['fieldvaluetype']})" if f.get("fieldvaluetype") else ""
            parts.append(f"- `{f.get('scriptid', '?')}` ({f.get('fieldtype', '?')}): {f.get('name', '?')}{vtype}")

    # ── Item custom fields (custitem_*) ───────────────────────────
    if metadata.item_custom_fields and isinstance(metadata.item_custom_fields, list):
        fields = metadata.item_custom_fields[:_MAX_FIELDS_PER_SECTION]
        parts.append(f"\n## Custom Item Fields ({len(metadata.item_custom_fields)} total)")
        parts.append("Use on the `item` table:")
        for f in fields:
            parts.append(f"- `{f.get('scriptid', '?')}` ({f.get('fieldtype', '?')}): {f.get('name', '?')}")

    # ── Custom record types ───────────────────────────────────────
    if metadata.custom_record_types and isinstance(metadata.custom_record_types, list):
        parts.append(f"\n## Custom Record Types ({len(metadata.custom_record_types)} total)")
        for r in metadata.custom_record_types[:30]:
            desc = f" — {r['description'][:80]}" if r.get("description") else ""
            parts.append(f"- `{r.get('scriptid', '?')}`: {r.get('name', '?')}{desc}")

    # ── Custom lists ──────────────────────────────────────────────
    if metadata.custom_lists and isinstance(metadata.custom_lists, list):
        parts.append(f"\n## Custom Lists ({len(metadata.custom_lists)} total)")
        for cl in metadata.custom_lists[:30]:
            desc = f" — {cl['description'][:80]}" if cl.get("description") else ""
            parts.append(f"- `{cl.get('scriptid', '?')}`: {cl.get('name', '?')}{desc}")

    # ── Subsidiaries ──────────────────────────────────────────────
    if metadata.subsidiaries and isinstance(metadata.subsidiaries, list):
        active = [s for s in metadata.subsidiaries if s.get("isinactive") != "T"]
        if active:
            parts.append(f"\n## Subsidiaries ({len(active)} active)")
            for s in active:
                parent = f" (parent: {s['parent']})" if s.get("parent") else ""
                parts.append(f"- ID {s.get('id', '?')}: {s.get('name', '?')}{parent}")

    # ── Departments ───────────────────────────────────────────────
    if metadata.departments and isinstance(metadata.departments, list):
        active = [d for d in metadata.departments if d.get("isinactive") != "T"]
        if active:
            parts.append(f"\n## Departments ({len(active)} active)")
            for d in active:
                parent = f" (parent: {d['parent']})" if d.get("parent") else ""
                parts.append(f"- ID {d.get('id', '?')}: {d.get('name', '?')}{parent}")

    # ── Classes ───────────────────────────────────────────────────
    if metadata.classifications and isinstance(metadata.classifications, list):
        active = [c for c in metadata.classifications if c.get("isinactive") != "T"]
        if active:
            parts.append(f"\n## Classes ({len(active)} active)")
            for c in active:
                parent = f" (parent: {c['parent']})" if c.get("parent") else ""
                parts.append(f"- ID {c.get('id', '?')}: {c.get('name', '?')}{parent}")

    # ── Locations ─────────────────────────────────────────────────
    if metadata.locations and isinstance(metadata.locations, list):
        active = [loc for loc in metadata.locations if loc.get("isinactive") != "T"]
        if active:
            parts.append(f"\n## Locations ({len(active)} active)")
            for loc in active:
                parent = f" (parent: {loc['parent']})" if loc.get("parent") else ""
                parts.append(f"- ID {loc.get('id', '?')}: {loc.get('name', '?')}{parent}")

    # Only return if we actually added content beyond the header
    if len(parts) <= 1:
        return ""
    return "\n".join(parts)


def _build_suiteql_rules_section(profile: TenantProfile) -> str:
    parts = [
        "SUITEQL SYNTAX RULES (Oracle-style SQL):",
        "- Row limiting: use ROWNUM in WHERE clause, e.g. WHERE type = 'SalesOrd' AND ROWNUM <= 10 ORDER BY id DESC",
        "- NEVER use FETCH FIRST N ROWS ONLY or LIMIT — they are NOT supported",
        "- NEVER use 'internalid' — the correct column is 'id'",
        "- NEVER use 'mainline' — it is not a valid SuiteQL column",
        "- Only ONE WHERE clause per query — combine conditions with AND",
        "- Use NVL() instead of IFNULL() or COALESCE()",
        "- NO Common Table Expressions (CTEs / WITH clauses) — use subqueries instead",
        "- String literals use single quotes: 'value'",
        "- Date filtering: TO_DATE('2024-01-01', 'YYYY-MM-DD')",
        "- Common tables: transaction, transactionline, customer, item, vendor, account, subsidiary",
        "- Common transaction columns: id, tranid, trandate, type, status, entity, memo, "
        "foreigntotal, exchangerate, subsidiary, department, location, createddate",
        "",
        "TRANSACTIONLINE RULES:",
        "- Join: transactionline tl JOIN transaction t ON tl.transaction = t.id",
        "- Line columns: id, linesequencenumber, item, quantity, rate, rateamount, "
        "foreignamount, memo, isclosed",
        "- Filter item lines: tl.mainline = 'F' AND tl.taxline = 'F' (TEXT 'T'/'F')",
        "- NEVER use dot notation (tl.item.name) — JOIN instead: JOIN item i ON tl.item = i.id",
        "- NEVER use 'amount' on transactionline — use 'foreignamount' or 'netamount'",
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
        "- To query NetSuite data, prefer external MCP tools (prefixed with 'ext__') if available. "
        "These connect directly to NetSuite and are the most reliable option.\n"
        "- If no external MCP tools are available, use the netsuite_suiteql tool as fallback.\n"
        "- To discover custom field names before writing a query, call netsuite_get_metadata first.\n"
        "- If a query fails with 'Unknown identifier', call netsuite_get_metadata to look up "
        "correct field names, fix the query, and retry automatically.\n"
        "- You may call tools multiple times in sequence.\n"
        "- To refresh custom field metadata, call netsuite_refresh_metadata.\n"
        "- For local platform data (orders, payments, refunds, payouts, disputes), "
        "use data_sample_table_read.\n"
        "- For documentation, use rag_search.\n"
        "- SuiteQL pagination: use FETCH FIRST N ROWS ONLY (NOT LIMIT)."
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
    metadata: NetSuiteMetadata | None = None,
) -> tuple[str, dict]:
    """Generate a deterministic system prompt template from structured profile data.

    Returns (template_text, sections_dict).
    """
    sections = {
        "identity": _build_identity_section(profile),
        "netsuite_context": _build_netsuite_context_section(profile),
        "netsuite_customizations": _build_netsuite_customizations_section(metadata),
        "suiteql_rules": _build_suiteql_rules_section(profile),
        "tool_rules": _build_tool_rules_section(),
        "policy_constraints": _build_policy_constraints_section(policy),
        "response_rules": _build_response_rules_section(),
    }

    # Combine non-empty sections in order
    section_order = [
        "identity",
        "netsuite_context",
        "netsuite_customizations",
        "suiteql_rules",
        "tool_rules",
        "policy_constraints",
        "response_rules",
    ]
    parts = []
    for key in section_order:
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

    # Get active metadata for custom field injection
    from app.services.netsuite_metadata_service import get_active_metadata

    metadata = await get_active_metadata(db, tenant_id)

    template_text, sections = generate_template(profile, policy, metadata)

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
        has_metadata=metadata is not None,
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
