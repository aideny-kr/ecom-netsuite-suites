"""Schema file completeness checks (TDD Cycle 8)."""

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


def test_transactionline_schema_has_key_columns():
    with open(os.path.join(SCHEMA_DIR, "transactionline.yaml")) as f:
        data = yaml.safe_load(f)
    col_names = {c["name"] for c in data["columns"]}
    required_cols = {"id", "transaction", "item", "quantity", "amount", "foreignamount", "mainline"}
    missing = required_cols - col_names
    assert not missing, f"transactionline.yaml missing columns: {missing}"


def test_each_column_has_description():
    for yaml_file in os.listdir(SCHEMA_DIR):
        if not yaml_file.endswith(".yaml"):
            continue
        with open(os.path.join(SCHEMA_DIR, yaml_file)) as f:
            data = yaml.safe_load(f)
        for col in data.get("columns", []):
            assert "description" in col and col["description"], (
                f"{yaml_file}: column '{col.get('name', '?')}' missing description"
            )


def test_table_name_matches_filename():
    for yaml_file in os.listdir(SCHEMA_DIR):
        if not yaml_file.endswith(".yaml"):
            continue
        with open(os.path.join(SCHEMA_DIR, yaml_file)) as f:
            data = yaml.safe_load(f)
        expected_name = yaml_file.replace(".yaml", "")
        assert data["table_name"] == expected_name, (
            f"{yaml_file}: table_name '{data['table_name']}' doesn't match filename"
        )
