"""Tests for the table schema loader service (TDD Cycle 2)."""

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


def test_dynamic_fields_excluded_from_xml():
    """Dynamic fields (custbody_*, etc.) should NOT appear in XML output."""
    schema = TableSchema(
        table_name="test",
        columns=[
            ColumnDef(name="id", type="integer", description="Primary key"),
            ColumnDef(name="custbody_x", type="text", description="Custom", dynamic=True),
        ],
    )
    xml = format_schemas_as_xml([schema])
    assert "id" in xml
    assert "custbody_x" not in xml


def test_merge_skips_fields_without_scriptid():
    """Custom fields without scriptid should be skipped."""
    schema = TableSchema(table_name="test", columns=[ColumnDef(name="id")])
    custom_fields = [
        {"scriptid": "custbody_valid", "name": "Valid"},
        {"name": "No Script ID"},  # Missing scriptid
        {"scriptid": "", "name": "Empty Script ID"},  # Empty scriptid
    ]
    merged = merge_custom_fields(schema, "body", custom_fields)
    assert len(merged.columns) == 2  # original + valid only
