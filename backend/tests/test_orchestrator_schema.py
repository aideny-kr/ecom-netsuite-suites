"""Tests for schema injection in the orchestrator (TDD Cycle 5)."""

import pytest
from app.services.schema_context_selector import select_relevant_schemas


def test_schema_context_assembled():
    """Orchestrator should select relevant schemas and add to context."""
    tables = select_relevant_schemas("how many open sales orders by vendor")
    assert "transaction" in tables
    assert "vendor" in tables


def test_schema_injected_into_context():
    """Context dict should include table_schemas key."""
    context: dict = {}
    context["table_schemas"] = "<standard_table_schemas>...</standard_table_schemas>"
    assert "table_schemas" in context
    assert context["table_schemas"].startswith("<standard_table_schemas>")


def test_schema_context_for_financial_query():
    """Financial queries should include accounting-related tables."""
    tables = select_relevant_schemas("show me the P&L for Q4")
    assert "transactionaccountingline" in tables
    assert "account" in tables
    assert "transaction" in tables


def test_schema_context_for_inventory_query():
    """Inventory queries should include item and inventory tables."""
    tables = select_relevant_schemas("what items are low on stock")
    assert "item" in tables
    assert "inventoryitemlocations" in tables
