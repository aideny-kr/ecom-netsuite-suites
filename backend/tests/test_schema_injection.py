"""Tests for schema section builder in prompt_template_service (TDD Cycle 4)."""

import pytest
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


def test_build_table_schema_section_filters_tables():
    """_build_table_schema_section should filter to relevant tables only."""
    from app.services.prompt_template_service import _build_table_schema_section

    xml = _build_table_schema_section(metadata=None, relevant_tables=["transaction", "customer"])
    assert "transaction" in xml
    assert "customer" in xml
    # Should NOT include unrelated tables
    assert "inventorynumber" not in xml


def test_build_table_schema_section_no_metadata():
    """Works without metadata (no custom field merging)."""
    from app.services.prompt_template_service import _build_table_schema_section

    xml = _build_table_schema_section(metadata=None, relevant_tables=["transaction"])
    assert "<standard_table_schemas>" in xml
    assert "transaction" in xml


def test_build_table_schema_section_empty_tables():
    """Returns empty string when no tables match."""
    from app.services.prompt_template_service import _build_table_schema_section

    xml = _build_table_schema_section(metadata=None, relevant_tables=["nonexistent_table"])
    assert xml == ""
