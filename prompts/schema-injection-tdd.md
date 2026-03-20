# Explicit Schema Injection — Anti-Hallucination Sprint 2 (TDD)

> Injects actual table column names and types into the agent prompt so the LLM
> stops guessing field names. Curated YAML files for standard tables + dynamic
> merge of custom fields from tenant metadata + contextual selection so only
> relevant schemas are injected per query.
>
> Use Red-Green-Refactor TDD for each cycle.

Read `CLAUDE.md` before starting. Follow all conventions exactly.

---

## Why This Matters

The #1 source of SuiteQL hallucination is wrong column names. The agent currently
knows custom fields from `netsuite_metadata` but NOT standard table columns. It
guesses `total_amount` when the real field is `amount`, or `customer_name` when
it's `companyname`. This sprint eliminates that class of error entirely.

## What Exists Today

- `prompt_template_service.py` line 69: `_build_netsuite_customizations_section()` formats custom fields into prompt text
- `prompt_template_service.py` line 23: `_MAX_FIELDS_PER_SECTION = 60` caps token budget per category
- `unified_agent.py` line 67: `{{INJECT_METADATA_HERE}}` placeholder in `<tenant_schema>` block
- `unified_agent.py` line 176: `PREFLIGHT SCHEMA CHECK — MANDATORY` with safe column list already hardcoded
- `orchestrator.py` line 498: concurrent context assembly (vernacular, domain knowledge, patterns)
- `netsuite_metadata` model: stores `transaction_body_fields`, `transaction_column_fields`, `entity_custom_fields`, `item_custom_fields`, `custom_record_types`, `custom_record_fields`
- `tenant_resolver.py`: entity resolution returns entity types (customer, vendor, item, etc.)

## Architecture

```
YAML schema files (knowledge/table_schemas/*.yaml)
  + Custom fields from netsuite_metadata (dynamic per tenant)
  → schema_context_selector picks relevant tables
  → table_schema_loader merges and formats
  → Injected as <standard_table_schemas> XML block in unified agent prompt
```

---

## TDD Cycles (8 cycles, 3 phases)

### Phase 1: Schema Files + Loader

**Cycle 1 — YAML Schema Files** (NEW directory)

Create `knowledge/table_schemas/` with YAML files for the most-used NetSuite tables.
Each file follows this format:

```yaml
# knowledge/table_schemas/transaction.yaml
table_name: transaction
description: "Core transaction record — sales orders, invoices, purchase orders, vendor bills, etc."
columns:
  - name: id
    type: integer
    description: "Internal ID (primary key, sequential — higher = newer)"
  - name: tranid
    type: text
    description: "Transaction number (e.g., SO-12345, INV-789, PO-456)"
  - name: trandate
    type: date
    description: "Transaction date"
  - name: type
    type: text
    description: "Type code: SalesOrd, CustInvc, VendBill, PurchOrd, CustCred, etc."
  - name: status
    type: text
    description: "Single-letter status: A=Pending Approval, B=Pending Fulfillment, C=Cancelled, etc."
  - name: entity
    type: integer
    description: "FK to customer/vendor record. Use BUILTIN.DF(t.entity) for display name."
  - name: total
    type: decimal
    description: "Header total in BASE currency (USD). Use SUM(t.total) for revenue."
  - name: foreigntotal
    type: decimal
    description: "Header total in TRANSACTION currency (could be EUR/GBP). Do NOT use for USD totals."
  - name: subsidiary
    type: integer
    description: "FK to subsidiary. Use BUILTIN.DF(t.subsidiary) for name."
  - name: department
    type: integer
    description: "FK to department. Use BUILTIN.DF(t.department) for name."
  - name: class
    type: integer
    description: "FK to classification. Use BUILTIN.DF(t.class) for name."
  - name: location
    type: integer
    description: "FK to location. Use BUILTIN.DF(t.location) for name."
  - name: memo
    type: text
    description: "Free-text memo field"
  - name: createddate
    type: datetime
    description: "Record creation timestamp"
  - name: lastmodifieddate
    type: datetime
    description: "Last modified timestamp"
  - name: currency
    type: integer
    description: "FK to currency record. Use BUILTIN.DF(t.currency) for code."
  - name: exchangerate
    type: decimal
    description: "Exchange rate used for this transaction"
  - name: duedate
    type: date
    description: "Due date (header level)"
  - name: shipdate
    type: date
    description: "Ship date"
  - name: terms
    type: integer
    description: "Payment terms FK. Use BUILTIN.DF(t.terms)."
  - name: approvalstatus
    type: integer
    description: "Approval status FK. Use BUILTIN.DF(t.approvalstatus)."
  - name: custbody_*
    type: varies
    description: "Custom body fields — see <tenant_schema> for this tenant's specific custbody fields"
    dynamic: true
common_joins:
  - partner: transactionline
    alias: tl
    on: "t.id = tl.transaction"
    use_when: "Need line-level details (items, quantities, amounts)"
  - partner: customer
    alias: c
    on: "t.entity = c.id"
    use_when: "Need customer details (name, email). Only for customer-type transactions."
  - partner: vendor
    alias: v
    on: "t.entity = v.id"
    use_when: "Need vendor details. Only for vendor-type transactions (POs, bills)."
```

Create schemas for these 19 tables (create all of them):

1. `transaction.yaml` — core transaction header
2. `transactionline.yaml` — line items (quantity, amount, item FK)
3. `transactionaccountingline.yaml` — GL posting lines (account, debit, credit)
4. `customer.yaml` — customer/company records
5. `vendor.yaml` — vendor records
6. `employee.yaml` — employee records
7. `item.yaml` — inventory/non-inventory/service items
8. `inventoryitemlocations.yaml` — inventory by location
9. `account.yaml` — chart of accounts
10. `subsidiary.yaml` — subsidiary hierarchy
11. `department.yaml` — department hierarchy
12. `classification.yaml` — class/classification records
13. `location.yaml` — location records
14. `currency.yaml` — currency definitions
15. `contact.yaml` — contacts
16. `salesrep.yaml` — sales rep records (FK on transaction)
17. `nexus.yaml` — tax nexus records
18. `inventorynumber.yaml` — serial/lot numbers
19. `customrecord_template.yaml` — template showing custom record schema pattern

**IMPORTANT**: Populate each YAML with REAL NetSuite SuiteQL column names. Use the
hardcoded safe columns from `unified_agent.py` line 179 as a starting point, then
expand with commonly used columns from the golden dataset files in `knowledge/golden_dataset/`.

---

**Cycle 2 — Schema Loader Service** (NEW file)

RED — Create `backend/tests/test_table_schema_loader.py`:
```python
import pytest
from app.services.table_schema_loader import (
    load_standard_schemas,
    TableSchema,
    ColumnDef,
    merge_custom_fields,
    format_schemas_as_xml,
)

def test_load_standard_schemas():
    schemas = load_standard_schemas()
    assert len(schemas) >= 19
    assert any(s.table_name == "transaction" for s in schemas)

def test_transaction_schema_has_key_columns():
    schemas = load_standard_schemas()
    txn = next(s for s in schemas if s.table_name == "transaction")
    col_names = {c.name for c in txn.columns}
    assert "id" in col_names
    assert "tranid" in col_names
    assert "trandate" in col_names
    assert "type" in col_names
    assert "status" in col_names
    assert "total" in col_names
    assert "entity" in col_names

def test_column_has_description():
    schemas = load_standard_schemas()
    txn = next(s for s in schemas if s.table_name == "transaction")
    id_col = next(c for c in txn.columns if c.name == "id")
    assert id_col.description is not None
    assert len(id_col.description) > 5

def test_merge_custom_fields():
    schemas = load_standard_schemas()
    txn = next(s for s in schemas if s.table_name == "transaction")
    original_count = len(txn.columns)

    custom_fields = [
        {"scriptid": "custbody_rush_flag", "name": "Rush Order Flag", "fieldtype": "checkbox"},
        {"scriptid": "custbody_source", "name": "Marketing Source", "fieldtype": "select"},
    ]
    merged = merge_custom_fields(txn, "transaction_body_fields", custom_fields)
    assert len(merged.columns) == original_count + 2
    assert any(c.name == "custbody_rush_flag" for c in merged.columns)

def test_format_schemas_as_xml():
    schemas = load_standard_schemas()
    xml = format_schemas_as_xml(schemas[:3])  # Just first 3 for test
    assert "<standard_table_schemas>" in xml
    assert "<table name=" in xml
    assert "</standard_table_schemas>" in xml

def test_format_respects_token_budget():
    schemas = load_standard_schemas()
    xml = format_schemas_as_xml(schemas, max_tokens=500)
    # Should truncate or summarize to stay under budget
    words = xml.split()
    assert len(words) <= 700  # ~1.4 tokens per word rough estimate

def test_schema_template_for_custom_records():
    schemas = load_standard_schemas()
    template = next((s for s in schemas if s.table_name == "customrecord_template"), None)
    assert template is not None
    assert template.description  # Should explain how custom records work
```

GREEN — Create `backend/app/services/table_schema_loader.py`:
```python
"""Table schema loader — reads curated YAML schemas and merges tenant custom fields.

Standard NetSuite table schemas are stored as YAML in knowledge/table_schemas/.
Custom fields from netsuite_metadata are dynamically merged at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Path to schema files (relative to project root)
_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "knowledge" / "table_schemas"

# Token budget for schema injection (prevents prompt bloat)
_DEFAULT_MAX_TOKENS = 5000


@dataclass
class ColumnDef:
    """A single column in a table schema."""
    name: str
    type: str = "text"
    description: str = ""
    dynamic: bool = False  # True for custbody_*, custcol_*, etc.


@dataclass
class JoinDef:
    """A common JOIN partner for a table."""
    partner: str
    alias: str
    on: str
    use_when: str = ""


@dataclass
class TableSchema:
    """Schema definition for a single NetSuite table."""
    table_name: str
    description: str = ""
    columns: list[ColumnDef] = field(default_factory=list)
    common_joins: list[JoinDef] = field(default_factory=list)


def load_standard_schemas() -> list[TableSchema]:
    """Load all YAML schema files from knowledge/table_schemas/.

    Returns list of TableSchema objects sorted by table name.
    """
    schemas: list[TableSchema] = []
    if not _SCHEMA_DIR.exists():
        return schemas

    for yaml_file in sorted(_SCHEMA_DIR.glob("*.yaml")):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        if not data or "table_name" not in data:
            continue

        columns = [
            ColumnDef(
                name=c["name"],
                type=c.get("type", "text"),
                description=c.get("description", ""),
                dynamic=c.get("dynamic", False),
            )
            for c in data.get("columns", [])
        ]
        joins = [
            JoinDef(
                partner=j["partner"],
                alias=j.get("alias", j["partner"][:2]),
                on=j["on"],
                use_when=j.get("use_when", ""),
            )
            for j in data.get("common_joins", [])
        ]
        schemas.append(
            TableSchema(
                table_name=data["table_name"],
                description=data.get("description", ""),
                columns=columns,
                common_joins=joins,
            )
        )
    return schemas


def merge_custom_fields(
    schema: TableSchema,
    field_category: str,
    custom_fields: list[dict[str, Any]],
) -> TableSchema:
    """Merge tenant-specific custom fields into a standard schema.

    Args:
        schema: The base table schema.
        field_category: Category name (e.g., "transaction_body_fields").
        custom_fields: List of field dicts from netsuite_metadata.

    Returns:
        New TableSchema with custom fields appended.
    """
    extra_columns = [
        ColumnDef(
            name=f.get("scriptid", "unknown"),
            type=f.get("fieldtype", "text"),
            description=f.get("name", "Custom field"),
            dynamic=True,
        )
        for f in custom_fields
        if f.get("scriptid")
    ]
    return TableSchema(
        table_name=schema.table_name,
        description=schema.description,
        columns=schema.columns + extra_columns,
        common_joins=schema.common_joins,
    )


def format_schemas_as_xml(
    schemas: list[TableSchema],
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> str:
    """Format table schemas as XML for injection into agent prompt.

    Respects max_tokens budget by truncating columns if needed.
    """
    parts: list[str] = ["<standard_table_schemas>"]
    token_estimate = 5  # opening + closing tags

    for schema in schemas:
        table_header = f'<table name="{schema.table_name}" description="{schema.description}">'
        parts.append(table_header)
        token_estimate += len(table_header.split()) * 1.4

        # Columns
        parts.append("  <columns>")
        for col in schema.columns:
            if col.dynamic:
                continue  # Dynamic fields shown in <tenant_schema>, not here
            line = f'    <col name="{col.name}" type="{col.type}">{col.description}</col>'
            line_tokens = len(line.split()) * 1.4
            if token_estimate + line_tokens > max_tokens:
                parts.append(f"    <!-- truncated: {len(schema.columns)} total columns -->")
                break
            parts.append(line)
            token_estimate += line_tokens
        parts.append("  </columns>")

        # Common joins (compact)
        if schema.common_joins:
            parts.append("  <joins>")
            for j in schema.common_joins:
                parts.append(
                    f'    <join table="{j.partner}" alias="{j.alias}" '
                    f'on="{j.on}">{j.use_when}</join>'
                )
                token_estimate += 15
            parts.append("  </joins>")

        parts.append("</table>")

        if token_estimate > max_tokens:
            parts.append(f"<!-- Budget exceeded. {len(schemas)} tables total, showing subset. -->")
            break

    parts.append("</standard_table_schemas>")
    return "\n".join(parts)
```

REFACTOR: None needed.

---

**Cycle 3 — Schema Context Selector** (NEW file)

RED — Create `backend/tests/test_schema_context_selector.py`:
```python
import pytest
from app.services.schema_context_selector import select_relevant_schemas

def test_customer_query_selects_customer_table():
    tables = select_relevant_schemas("show me all customers")
    assert "customer" in tables

def test_order_query_selects_transaction():
    tables = select_relevant_schemas("how many sales orders this month")
    assert "transaction" in tables

def test_line_detail_query_selects_transactionline():
    tables = select_relevant_schemas("show me line items on PO-12345")
    assert "transaction" in tables
    assert "transactionline" in tables

def test_inventory_query_selects_inventory():
    tables = select_relevant_schemas("what is our current inventory")
    assert "item" in tables
    assert "inventoryitemlocations" in tables

def test_financial_query_selects_accounting():
    tables = select_relevant_schemas("net income by account for Q4")
    assert "transactionaccountingline" in tables
    assert "account" in tables

def test_vendor_query_selects_vendor():
    tables = select_relevant_schemas("list all vendors with open POs")
    assert "vendor" in tables
    assert "transaction" in tables

def test_empty_question_returns_core_tables():
    tables = select_relevant_schemas("")
    # Should return at minimum the core tables as fallback
    assert "transaction" in tables

def test_entity_types_from_resolution():
    tables = select_relevant_schemas(
        "show me orders",
        entity_types=["customer"],
    )
    assert "customer" in tables
    assert "transaction" in tables

def test_max_tables_cap():
    # Even if many tables match, cap at reasonable number
    tables = select_relevant_schemas(
        "everything about orders, customers, vendors, items, inventory, employees, GL"
    )
    assert len(tables) <= 10

def test_custom_record_passthrough():
    tables = select_relevant_schemas(
        "show me custom record data",
        custom_record_names=["customrecord_inv_processor"],
    )
    assert "customrecord_inv_processor" in tables
```

GREEN — Create `backend/app/services/schema_context_selector.py`:
```python
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
        re.compile(r"(?xi)\b(?:customer|buyer|client|purchaser|companyname)\b"),
    ),
    # Vendor
    (
        {"vendor"},
        re.compile(r"(?xi)\b(?:vendor|supplier|manufacturer)\b"),
    ),
    # Employee
    (
        {"employee"},
        re.compile(r"(?xi)\b(?:employee|staff|worker|team\s+member|hire)\b"),
    ),
    # Item / product
    (
        {"item"},
        re.compile(r"(?xi)\b(?:item|product|sku|inventory\s+item|goods|merchandise)\b"),
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
                order | invoice | bill | purchase\s+order |
                sales\s+order | credit\s+memo | return |
                transaction | receipt | payment | deposit |
                revenue | sales | expense | cost
            )\b"""
        ),
    ),
    # Subsidiary / org structure
    (
        {"subsidiary"},
        re.compile(r"(?xi)\b(?:subsidiary|subsidiaries|entity|company|division)\b"),
    ),
    # Department / class / location (dimensions)
    (
        {"department"},
        re.compile(r"(?xi)\b(?:department)\b"),
    ),
    (
        {"classification"},
        re.compile(r"(?xi)\b(?:class(?:ification)?|category)\b"),
    ),
    (
        {"location"},
        re.compile(r"(?xi)\b(?:location|warehouse|store|facility)\b"),
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
```

REFACTOR: None needed.

---

### Phase 2: Prompt Integration

**Cycle 4 — Schema Section Builder in Prompt Template Service**

RED — Create `backend/tests/test_schema_injection.py`:
```python
import pytest
from unittest.mock import MagicMock
from app.services.table_schema_loader import load_standard_schemas, merge_custom_fields, format_schemas_as_xml

def test_build_schema_section_with_custom_fields():
    """Schema section includes both standard columns and tenant custom fields."""
    schemas = load_standard_schemas()
    txn = next(s for s in schemas if s.table_name == "transaction")

    # Simulate tenant custom fields from netsuite_metadata
    custom_fields = [
        {"scriptid": "custbody_rush_flag", "name": "Rush Order", "fieldtype": "checkbox"},
    ]
    merged = merge_custom_fields(txn, "transaction_body_fields", custom_fields)
    xml = format_schemas_as_xml([merged])

    assert "custbody_rush_flag" not in xml  # Dynamic fields shown in tenant_schema, not here
    assert "tranid" in xml  # Standard columns present
    assert "transaction" in xml

def test_schema_xml_is_well_formed():
    schemas = load_standard_schemas()
    xml = format_schemas_as_xml(schemas[:5])
    assert xml.startswith("<standard_table_schemas>")
    assert xml.endswith("</standard_table_schemas>")
    assert xml.count("<table ") == xml.count("</table>")

def test_schema_section_respects_budget():
    schemas = load_standard_schemas()
    xml = format_schemas_as_xml(schemas, max_tokens=1000)
    # Should be under budget
    words = xml.split()
    assert len(words) < 1500  # Generous margin
```

GREEN — Add to `backend/app/services/prompt_template_service.py`:

After `_build_netsuite_customizations_section()`, add a new function:
```python
def _build_table_schema_section(
    metadata: NetSuiteMetadata | None,
    relevant_tables: list[str] | None = None,
) -> str:
    """Build <standard_table_schemas> XML block from curated schemas + tenant custom fields.

    Args:
        metadata: Tenant metadata for custom field merging.
        relevant_tables: Table names to include (from schema_context_selector).
                        If None, includes all schemas.
    """
    from app.services.table_schema_loader import (
        load_standard_schemas,
        merge_custom_fields,
        format_schemas_as_xml,
    )

    all_schemas = load_standard_schemas()

    # Filter to relevant tables if specified
    if relevant_tables:
        relevant_set = set(relevant_tables)
        schemas = [s for s in all_schemas if s.table_name in relevant_set]
    else:
        schemas = all_schemas

    # Merge tenant custom fields if metadata available
    if metadata:
        _FIELD_MAP = {
            "transaction": ("transaction_body_fields", metadata.transaction_body_fields),
            "transactionline": ("transaction_column_fields", metadata.transaction_column_fields),
            "customer": ("entity_custom_fields", metadata.entity_custom_fields),
            "vendor": ("entity_custom_fields", metadata.entity_custom_fields),
            "employee": ("entity_custom_fields", metadata.entity_custom_fields),
            "item": ("item_custom_fields", metadata.item_custom_fields),
        }
        merged_schemas = []
        for schema in schemas:
            mapping = _FIELD_MAP.get(schema.table_name)
            if mapping and mapping[1] and isinstance(mapping[1], list):
                merged_schemas.append(merge_custom_fields(schema, mapping[0], mapping[1]))
            else:
                merged_schemas.append(schema)
        schemas = merged_schemas

    if not schemas:
        return ""

    return format_schemas_as_xml(schemas)
```

REFACTOR: None needed.

---

**Cycle 5 — Orchestrator Integration**

RED — Create `backend/tests/test_orchestrator_schema.py`:
```python
import pytest

def test_schema_context_assembled():
    """Orchestrator should select relevant schemas and add to context."""
    from app.services.schema_context_selector import select_relevant_schemas

    tables = select_relevant_schemas("how many open sales orders by vendor")
    assert "transaction" in tables
    assert "vendor" in tables

def test_schema_injected_into_context():
    """Context dict should include table_schemas key."""
    context = {}
    context["table_schemas"] = "<standard_table_schemas>...</standard_table_schemas>"
    assert "table_schemas" in context
    assert context["table_schemas"].startswith("<standard_table_schemas>")
```

GREEN — Modify `backend/app/services/chat/orchestrator.py`:

1. After the concurrent context assembly (around line 508), add schema selection:
```python
    # Select relevant table schemas based on query
    from app.services.schema_context_selector import select_relevant_schemas

    relevant_tables = select_relevant_schemas(
        sanitized_input,
        entity_types=[],  # Could extract from vernacular_result later
    )
    print(f"[ORCHESTRATOR] Schema tables selected: {relevant_tables}", flush=True)
```

2. After building the context dict (around line 547), add schema injection:
```python
    # Build schema XML for injection
    from app.services.prompt_template_service import _build_table_schema_section

    schema_xml = _build_table_schema_section(
        metadata=metadata,  # from get_active_metadata() earlier
        relevant_tables=relevant_tables,
    )
    if schema_xml:
        context["table_schemas"] = schema_xml
        print(f"[ORCHESTRATOR] Schema injected ({len(schema_xml)} chars, {len(relevant_tables)} tables)", flush=True)
```

REFACTOR: None needed.

---

**Cycle 6 — Unified Agent Prompt Update**

RED — Test that agent system prompt includes schema block when provided in context:
```python
def test_system_prompt_includes_table_schemas():
    """When context has table_schemas, it should appear in final system prompt."""
    context = {
        "table_schemas": "<standard_table_schemas><table name=\"transaction\">...</table></standard_table_schemas>"
    }
    # The unified agent should inject this into its system prompt
    assert "standard_table_schemas" in context["table_schemas"]
```

GREEN — Modify `backend/app/services/chat/agents/unified_agent.py`:

1. In the system prompt assembly (where `{{INJECT_METADATA_HERE}}` is replaced), also inject the table schemas. Add a new placeholder or append after the existing tenant_schema block:

After the `<tenant_context>` block (around line 70), add:
```python
{{INJECT_TABLE_SCHEMAS}}
```

2. In the agent's `__init__` or `_build_system_prompt()` method, inject:
```python
    table_schemas = self._context.get("table_schemas", "")
    prompt = prompt.replace("{{INJECT_TABLE_SCHEMAS}}", table_schemas)
```

3. Strengthen the anti-hallucination guard in `<how_to_think>` (line 73-82). Update rule 4:
```
4. **ANTI-HALLUCINATION**: You have access to <standard_table_schemas> with REAL column names and types.
   - ONLY use columns listed in <standard_table_schemas> or <tenant_schema>.
   - If a column is NOT listed, call netsuite_get_metadata to verify it exists BEFORE using it.
   - NEVER guess or invent column names. Wrong column names waste steps and return 400 errors.
   - For custom fields (custbody_*, custcol_*, custentity_*, custitem_*), verify in <tenant_schema>.
```

REFACTOR: None needed.

---

### Phase 3: Verification

**Cycle 7 — Integration Test**

RED — Create `backend/tests/test_schema_injection_integration.py`:
```python
import pytest
from app.services.table_schema_loader import load_standard_schemas, format_schemas_as_xml
from app.services.schema_context_selector import select_relevant_schemas

def test_full_schema_pipeline():
    """End-to-end: question → select tables → load schemas → format XML."""
    # 1. Classify
    tables = select_relevant_schemas("total revenue by subsidiary this quarter")

    # 2. Load
    all_schemas = load_standard_schemas()
    selected = [s for s in all_schemas if s.table_name in tables]

    # 3. Format
    xml = format_schemas_as_xml(selected)

    # Verify
    assert "transaction" in xml
    assert "subsidiary" in xml
    assert "total" in xml  # transaction.total column
    assert len(xml) > 100

def test_schema_pipeline_for_inventory():
    tables = select_relevant_schemas("what items are in stock at warehouse A")
    all_schemas = load_standard_schemas()
    selected = [s for s in all_schemas if s.table_name in tables]
    xml = format_schemas_as_xml(selected)

    assert "inventoryitemlocations" in xml or "item" in xml

def test_schema_token_budget_real_data():
    """With all 19 schemas, verify token budget is respected."""
    schemas = load_standard_schemas()
    xml = format_schemas_as_xml(schemas, max_tokens=5000)
    word_count = len(xml.split())
    print(f"Schema XML: {word_count} words, {len(xml)} chars")
    # Should be well under 5000 tokens (~3500 words)
    assert word_count < 5000
```

GREEN — All previous implementations should make these pass.

---

**Cycle 8 — Schema File Completeness Check**

RED — Verify all 19 schema files exist and have required fields:
```python
import os
import yaml
import pytest

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "knowledge", "table_schemas")

def test_all_schema_files_exist():
    required = [
        "transaction", "transactionline", "transactionaccountingline",
        "customer", "vendor", "employee", "item", "inventoryitemlocations",
        "account", "subsidiary", "department", "classification", "location",
        "currency", "contact", "salesrep", "nexus", "inventorynumber",
        "customrecord_template",
    ]
    for table in required:
        path = os.path.join(SCHEMA_DIR, f"{table}.yaml")
        assert os.path.exists(path), f"Missing schema: {table}.yaml"

def test_each_schema_has_columns():
    for yaml_file in os.listdir(SCHEMA_DIR):
        if not yaml_file.endswith(".yaml"):
            continue
        with open(os.path.join(SCHEMA_DIR, yaml_file)) as f:
            data = yaml.safe_load(f)
        assert "table_name" in data, f"{yaml_file} missing table_name"
        assert "columns" in data, f"{yaml_file} missing columns"
        assert len(data["columns"]) >= 3, f"{yaml_file} has too few columns"

def test_transaction_schema_comprehensive():
    with open(os.path.join(SCHEMA_DIR, "transaction.yaml")) as f:
        data = yaml.safe_load(f)
    col_names = {c["name"] for c in data["columns"]}
    required_cols = {"id", "tranid", "trandate", "type", "status", "entity", "total", "foreigntotal", "memo"}
    missing = required_cols - col_names
    assert not missing, f"transaction.yaml missing columns: {missing}"
```

GREEN — All 19 YAML files with complete column definitions.

---

## Files to Create (22 new)

| File | Purpose |
|------|---------|
| `knowledge/table_schemas/transaction.yaml` | Transaction header schema |
| `knowledge/table_schemas/transactionline.yaml` | Transaction line schema |
| `knowledge/table_schemas/transactionaccountingline.yaml` | GL posting lines schema |
| `knowledge/table_schemas/customer.yaml` | Customer records schema |
| `knowledge/table_schemas/vendor.yaml` | Vendor records schema |
| `knowledge/table_schemas/employee.yaml` | Employee records schema |
| `knowledge/table_schemas/item.yaml` | Item/product schema |
| `knowledge/table_schemas/inventoryitemlocations.yaml` | Inventory by location schema |
| `knowledge/table_schemas/account.yaml` | Chart of accounts schema |
| `knowledge/table_schemas/subsidiary.yaml` | Subsidiary hierarchy schema |
| `knowledge/table_schemas/department.yaml` | Department hierarchy schema |
| `knowledge/table_schemas/classification.yaml` | Class/classification schema |
| `knowledge/table_schemas/location.yaml` | Location records schema |
| `knowledge/table_schemas/currency.yaml` | Currency definitions schema |
| `knowledge/table_schemas/contact.yaml` | Contact records schema |
| `knowledge/table_schemas/salesrep.yaml` | Sales rep schema |
| `knowledge/table_schemas/nexus.yaml` | Tax nexus schema |
| `knowledge/table_schemas/inventorynumber.yaml` | Serial/lot number schema |
| `knowledge/table_schemas/customrecord_template.yaml` | Custom record template |
| `backend/app/services/table_schema_loader.py` | YAML parser + merger + formatter |
| `backend/app/services/schema_context_selector.py` | Relevant table picker |
| `backend/tests/test_table_schema_loader.py` | Loader unit tests |

## Files to Modify (4 existing)

| File | Change |
|------|--------|
| `backend/app/services/prompt_template_service.py` | Add `_build_table_schema_section()` |
| `backend/app/services/chat/agents/unified_agent.py` | Add `{{INJECT_TABLE_SCHEMAS}}` + strengthen anti-hallucination guard |
| `backend/app/services/chat/orchestrator.py` | Call schema selector, build XML, inject into context |
| `backend/tests/test_schema_context_selector.py` | Context selector unit tests |

## Dependencies

- `pyyaml` (already in requirements — used by other parts of the codebase)
- Sprint 1 (query importance ranking) should be done first but is not strictly required

## Verification

1. `pytest backend/tests/test_table_schema_loader.py -v` — all loader tests pass
2. `pytest backend/tests/test_schema_context_selector.py -v` — all selector tests pass
3. `pytest backend/tests/test_schema_injection.py -v` — section builder tests pass
4. `pytest backend/tests/test_schema_injection_integration.py -v` — end-to-end pipeline passes
5. Verbose logging: send a chat message, grep logs for `[ORCHESTRATOR] Schema tables selected:` and `[ORCHESTRATOR] Schema injected`
6. Manual: ask agent to use a non-existent column, verify it calls `netsuite_get_metadata` instead of guessing
