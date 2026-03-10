"""Integration tests for the full schema injection pipeline (TDD Cycle 7)."""

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


def test_pipeline_with_entity_types():
    """Entity types from resolution should influence table selection."""
    tables = select_relevant_schemas(
        "show me recent orders",
        entity_types=["customer"],
    )
    all_schemas = load_standard_schemas()
    selected = [s for s in all_schemas if s.table_name in tables]
    xml = format_schemas_as_xml(selected)

    assert "customer" in xml
    assert "transaction" in xml


def test_pipeline_with_custom_records():
    """Custom record names should be passed through even without YAML files."""
    tables = select_relevant_schemas(
        "query custom record",
        custom_record_names=["customrecord_inv_processor"],
    )
    assert "customrecord_inv_processor" in tables
