"""Tests for tenant_entity_seeder._build_rows — verifies all entity types are extracted correctly."""

import uuid

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
        "scripts",
        "script_deployments",
        "workflows",
    ]:
        setattr(md, attr, kwargs.get(attr))
    return md


TENANT_ID = uuid.uuid4()


class TestBuildRowsCustomLists:
    def test_custom_list_rows_have_required_fields(self):
        md = _make_metadata(
            custom_lists=[
                {"scriptid": "customlist_order_status", "name": "Order Status", "description": "Status list"},
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "customlist"
        assert row["natural_name"] == "Order Status"
        assert row["script_id"] == "customlist_order_status"
        assert row["tenant_id"] == TENANT_ID

    def test_custom_list_skips_non_customlist_scriptid(self):
        md = _make_metadata(
            custom_lists=[
                {"scriptid": "some_other_thing", "name": "Not a list"},
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 0


class TestBuildRowsCustomListValues:
    def test_custom_list_values_seeded(self):
        md = _make_metadata(
            custom_list_values={
                "customlist_integration_status": [
                    {"id": 1, "name": "Pending"},
                    {"id": 2, "name": "Completed"},
                    {"id": 3, "name": "Failed"},
                ]
            }
        )
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
        md = _make_metadata(
            saved_searches=[
                {"id": 100, "title": "Open Orders Report", "recordtype": "transaction", "owner": "Admin"},
            ]
        )
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
        md = _make_metadata(
            custom_record_types=[
                {"scriptid": "customrecord_r_inv_processor", "name": "Inventory Processor", "description": "Inv proc"},
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "customrecord"
        assert rows[0]["script_id"] == "customrecord_r_inv_processor"


class TestBuildRowsBodyFields:
    def test_body_fields_seeded_with_fieldtype(self):
        md = _make_metadata(
            transaction_body_fields=[
                {"scriptid": "custbody_order_status", "name": "Order Status", "fieldtype": "SELECT"},
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "transactionbodyfield"
        assert "SELECT" in rows[0]["description"]


class TestBuildRowsScripts:
    def test_scripts_seeded(self):
        md = _make_metadata(
            scripts=[
                {
                    "scriptid": "customscript_order_proc",
                    "name": "Order Processor",
                    "scripttype": "USEREVENT",
                    "description": "Processes orders",
                },
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "script"
        assert rows[0]["script_id"] == "customscript_order_proc"
        assert rows[0]["natural_name"] == "Order Processor"
        assert "USEREVENT" in rows[0]["description"]
        assert "Processes orders" in rows[0]["description"]

    def test_script_missing_name_skipped(self):
        md = _make_metadata(scripts=[{"scriptid": "customscript_x", "name": ""}])
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 0


class TestBuildRowsScriptDeployments:
    def test_deployments_seeded(self):
        md = _make_metadata(
            script_deployments=[
                {
                    "scriptid": "customdeploy_order_proc",
                    "title": "Order Deploy",
                    "status": "RELEASED",
                    "recordtype": "salesorder",
                },
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "scriptdeployment"
        assert rows[0]["natural_name"] == "Order Deploy"
        assert "salesorder" in rows[0]["description"]

    def test_deployment_uses_scriptid_when_no_title(self):
        md = _make_metadata(
            script_deployments=[
                {"scriptid": "customdeploy_x", "status": "RELEASED", "recordtype": "invoice"},
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert rows[0]["natural_name"] == "customdeploy_x"


class TestBuildRowsWorkflows:
    def test_workflows_seeded(self):
        md = _make_metadata(
            workflows=[
                {
                    "scriptid": "customworkflow_approve_po",
                    "name": "Approve PO",
                    "recordtype": "purchaseorder",
                    "description": "PO approval",
                },
            ]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "workflow"
        assert rows[0]["script_id"] == "customworkflow_approve_po"
        assert "purchaseorder" in rows[0]["description"]
        assert "PO approval" in rows[0]["description"]

    def test_workflow_missing_name_skipped(self):
        md = _make_metadata(
            workflows=[{"scriptid": "customworkflow_x", "name": "", "recordtype": "salesorder"}]
        )
        rows = _build_rows(TENANT_ID, md)
        assert len(rows) == 0
