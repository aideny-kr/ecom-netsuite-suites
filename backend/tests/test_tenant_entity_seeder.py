"""Tests for tenant_entity_seeder._build_rows — verifies all entity types are extracted correctly."""

import uuid

import pytest

from app.services.tenant_entity_seeder import _build_rows


def _make_metadata(**kwargs):
    """Create a minimal metadata-like object with given attributes."""

    class FakeMetadata:
        pass

    md = FakeMetadata()
    for attr in [
        "custom_record_types",
        "transaction_body_fields",
        "transaction_column_fields",
        "entity_custom_fields",
        "item_custom_fields",
        "custom_record_fields",
        "custom_lists",
        "custom_list_values",
        "saved_searches",
    ]:
        setattr(md, attr, kwargs.get(attr))
    return md


TENANT_ID = uuid.uuid4()


class TestBuildRowsCustomLists:
    def test_custom_list_rows_have_required_fields(self):
        md = _make_metadata(custom_lists=[
            {"scriptid": "customlist_order_status", "name": "Order Status", "description": "Status list"},
        ])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "customlist"
        assert row["natural_name"] == "Order Status"
        assert row["script_id"] == "customlist_order_status"
        assert row["tenant_id"] == TENANT_ID

    def test_custom_list_skips_non_customlist_scriptid(self):
        md = _make_metadata(custom_lists=[
            {"scriptid": "some_other_thing", "name": "Not a list"},
        ])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 0


class TestBuildRowsCustomListValues:
    def test_custom_list_values_seeded(self):
        md = _make_metadata(custom_list_values={
            "customlist_integration_status": [
                {"id": 1, "name": "Pending"},
                {"id": 2, "name": "Completed"},
                {"id": 3, "name": "Failed"},
            ]
        })
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 3
        assert all(r["entity_type"] == "customlistvalue" for r in rows)
        failed = [r for r in rows if r["natural_name"] == "Failed"][0]
        assert failed["script_id"] == "customlist_integration_status.3"
        assert "Value for list" in failed["description"]

    def test_empty_list_values_skipped(self):
        md = _make_metadata(custom_list_values={"customlist_empty": []})
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 0


class TestBuildRowsSavedSearches:
    def test_saved_searches_seeded(self):
        md = _make_metadata(saved_searches=[
            {"id": 100, "title": "Open Orders Report", "recordtype": "transaction", "owner": "Admin"},
        ])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "savedsearch"
        assert row["natural_name"] == "Open Orders Report"
        assert row["script_id"] == "100"
        assert "transaction" in row["description"]

    def test_saved_search_missing_title_skipped(self):
        md = _make_metadata(saved_searches=[{"id": 100, "title": "", "recordtype": "transaction"}])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 0


class TestBuildRowsCustomRecordTypes:
    def test_custom_record_types_seeded(self):
        md = _make_metadata(custom_record_types=[
            {"scriptid": "customrecord_r_inv_processor", "name": "Inventory Processor", "description": "Inv proc"},
        ])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "customrecord"
        assert rows[0]["script_id"] == "customrecord_r_inv_processor"


class TestBuildRowsBodyFields:
    def test_body_fields_seeded_with_fieldtype(self):
        md = _make_metadata(transaction_body_fields=[
            {"scriptid": "custbody_order_status", "name": "Order Status", "fieldtype": "SELECT"},
        ])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "transactionbodyfield"
        assert "SELECT" in rows[0]["description"]
