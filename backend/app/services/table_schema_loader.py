"""Table schema loader — reads curated YAML schemas and merges tenant custom fields.

Standard NetSuite table schemas are stored as YAML in knowledge/table_schemas/.
Custom fields from netsuite_metadata are dynamically merged at runtime.
"""

from __future__ import annotations

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
                on=j.get("on") or j.get(True, ""),  # YAML parses bare `on:` as boolean True
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
