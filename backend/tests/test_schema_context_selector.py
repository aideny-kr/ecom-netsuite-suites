"""Tests for the schema context selector (TDD Cycle 3)."""

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


def test_transactionline_implies_transaction():
    """If transactionline is selected, transaction must also be included."""
    tables = select_relevant_schemas("show quantity by line item")
    assert "transactionline" in tables
    assert "transaction" in tables


def test_accounting_line_implies_transaction():
    """If TAL is selected, transaction must also be included."""
    tables = select_relevant_schemas("show me GL debits and credits")
    assert "transactionaccountingline" in tables
    assert "transaction" in tables


def test_subsidiary_query():
    tables = select_relevant_schemas("revenue by subsidiary")
    assert "subsidiary" in tables


def test_department_query():
    tables = select_relevant_schemas("expenses by department")
    assert "department" in tables


def test_currency_query():
    tables = select_relevant_schemas("multi-currency exchange rate report")
    assert "currency" in tables
