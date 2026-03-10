"""Schema context selector — picks relevant table schemas for a query.

Uses keyword heuristics to identify which tables the user's question
likely needs, preventing token bloat from injecting all 19 schemas.
"""

from __future__ import annotations

import re

# Max tables to inject (token budget guard)
_MAX_TABLES = 8

# Core tables always included as fallback
_CORE_TABLES = {"transaction"}

# Keyword-to-table mapping (checked in order, all matches collected)
_TABLE_RULES: list[tuple[set[str], re.Pattern[str]]] = [
    # Transaction line details
    (
        {"transaction", "transactionline"},
        re.compile(
            r"""(?xi)\b(?:
                line\s*items? | transactionline | line[\s-]level |
                quantity | qty | rate\b | (?:line|item)\s+amount |
                sku | fulfillment | shipment | receipt
            )\b"""
        ),
    ),
    # Financial / GL / accounting
    (
        {"transactionaccountingline", "account", "transaction"},
        re.compile(
            r"""(?xi)\b(?:
                gl | general\s+ledger | accounting\s+line |
                net\s+income | gross\s+(?:margin|profit) |
                p\s*[&/]\s*l | profit\s+(?:and|&)\s+loss |
                balance\s+sheet | income\s+statement |
                debit | credit | journal\s+entr |
                chart\s+of\s+accounts | account\s+(?:type|number|name|balance)
            )\b"""
        ),
    ),
    # Customer
    (
        {"customer"},
        re.compile(r"(?xi)\b(?:customers?|buyers?|clients?|purchasers?|companyname)\b"),
    ),
    # Vendor
    (
        {"vendor"},
        re.compile(r"(?xi)\b(?:vendors?|suppliers?|manufacturers?)\b"),
    ),
    # Employee
    (
        {"employee"},
        re.compile(r"(?xi)\b(?:employees?|staff|workers?|team\s+members?|hires?)\b"),
    ),
    # Item / product
    (
        {"item"},
        re.compile(r"(?xi)\b(?:items?|products?|sku|inventory\s+items?|goods|merchandise)\b"),
    ),
    # Inventory
    (
        {"inventoryitemlocations", "item"},
        re.compile(
            r"""(?xi)\b(?:
                inventory | stock | warehouse | on[\s-]hand |
                available\s+(?:qty|quantity) | reorder
            )\b"""
        ),
    ),
    # Transaction (general — orders, invoices, bills)
    (
        {"transaction"},
        re.compile(
            r"""(?xi)\b(?:
                orders? | invoices? | bills? | purchase\s+orders? | POs? |
                sales\s+orders? | SOs? | credit\s+memos? | returns? |
                transactions? | receipts? | payments? | deposits? |
                revenue | sales | expenses? | costs?
            )\b"""
        ),
    ),
    # Subsidiary / org structure
    (
        {"subsidiary"},
        re.compile(r"(?xi)\b(?:subsidiary|subsidiaries|division)\b"),
    ),
    # Department / class / location (dimensions)
    (
        {"department"},
        re.compile(r"(?xi)\b(?:departments?)\b"),
    ),
    (
        {"classification"},
        re.compile(r"(?xi)\b(?:class(?:ification)?|category)\b"),
    ),
    (
        {"location"},
        re.compile(r"(?xi)\b(?:locations?|warehouses?|stores?|facilities)\b"),
    ),
    # Currency
    (
        {"currency"},
        re.compile(r"(?xi)\b(?:currency|exchange\s+rate|forex|multi[\s-]currency)\b"),
    ),
]


def select_relevant_schemas(
    user_question: str,
    *,
    entity_types: list[str] | None = None,
    custom_record_names: list[str] | None = None,
) -> list[str]:
    """Select table names relevant to a user question.

    Args:
        user_question: The raw user question text.
        entity_types: Entity types from entity resolution (e.g., ["customer", "vendor"]).
        custom_record_names: Custom record table names to include.

    Returns:
        List of table names to inject schemas for (max _MAX_TABLES).
    """
    selected: set[str] = set()

    # 1. Keyword matching
    for tables, pattern in _TABLE_RULES:
        if pattern.search(user_question):
            selected.update(tables)

    # 2. Entity types from resolution
    if entity_types:
        for etype in entity_types:
            etype_lower = etype.lower()
            if etype_lower in ("customer", "vendor", "employee", "item"):
                selected.add(etype_lower)

    # 3. Custom records
    if custom_record_names:
        for cr_name in custom_record_names:
            if cr_name.startswith("customrecord_"):
                selected.add(cr_name)

    # 4. Fallback: if nothing matched, include core tables
    if not selected:
        selected = set(_CORE_TABLES)

    # 5. Always include transaction if transactionline or TAL is selected
    if "transactionline" in selected or "transactionaccountingline" in selected:
        selected.add("transaction")

    # 6. Cap at max
    return sorted(selected)[:_MAX_TABLES]
