"""Tests for netsuite_metadata_rag formatters."""

from app.services.netsuite_metadata_rag import (
    _format_custom_list_values,
    _format_saved_searches,
    _format_script_deployments,
    _format_scripts,
    _format_workflows,
)


class TestFormatCustomListValues:
    def test_basic_formatting(self):
        values = [
            {"id": 1, "name": "Pending"},
            {"id": 2, "name": "Completed"},
            {"id": 3, "name": "Failed", "isinactive": "T"},
        ]
        result = _format_custom_list_values("customlist_order_status", values)
        assert "customlist_order_status" in result
        assert "ID 1: Pending" in result
        assert "ID 3: Failed [INACTIVE]" in result
        assert "WHERE field_name" in result

    def test_empty_values(self):
        result = _format_custom_list_values("customlist_empty", [])
        assert "customlist_empty" in result


class TestFormatSavedSearches:
    def test_basic_formatting(self):
        searches = [
            {"id": 100, "title": "Open POs", "recordtype": "transaction", "owner": "Admin"},
            {"id": 200, "title": "Inventory Report", "recordtype": "item"},
        ]
        result = _format_saved_searches(searches)
        assert "ID 100: Open POs" in result
        assert "(owner: Admin)" in result
        assert "ID 200: Inventory Report" in result
        assert "Saved Searches" in result


class TestFormatScripts:
    def test_basic_formatting(self):
        scripts = [
            {
                "id": 1,
                "scriptid": "customscript_order_proc",
                "name": "Order Processor",
                "scripttype": "USEREVENT",
                "description": "Processes sales orders",
                "scriptfile": 42,
            },
        ]
        result = _format_scripts(scripts)
        assert "customscript_order_proc" in result
        assert "USEREVENT" in result
        assert "Processes sales orders" in result
        assert "(file: 42)" in result
        assert "SuiteScripts" in result

    def test_missing_optional_fields(self):
        scripts = [{"id": 1, "scriptid": "customscript_x", "name": "Test", "scripttype": "SCHEDULED"}]
        result = _format_scripts(scripts)
        assert "customscript_x" in result
        assert "SCHEDULED" in result
        assert "file:" not in result


class TestFormatScriptDeployments:
    def test_basic_formatting(self):
        deps = [
            {
                "script": 1,
                "scriptid": "customdeploy_order_proc",
                "title": "Order Deploy",
                "status": "RELEASED",
                "recordtype": "salesorder",
                "eventtype": "CREATE",
            },
        ]
        result = _format_script_deployments(deps)
        assert "customdeploy_order_proc" in result
        assert "Order Deploy" in result
        assert "salesorder" in result
        assert "event: CREATE" in result

    def test_missing_optional_fields(self):
        deps = [{"script": 1, "scriptid": "customdeploy_x", "status": "RELEASED", "recordtype": "invoice"}]
        result = _format_script_deployments(deps)
        assert "customdeploy_x" in result
        assert "event:" not in result


class TestFormatWorkflows:
    def test_basic_formatting(self):
        wfs = [
            {
                "scriptid": "customworkflow_approve_po",
                "name": "Approve PO",
                "recordtype": "purchaseorder",
                "status": "RELEASED",
                "description": "Approval workflow",
                "initoncreate": "T",
                "initonedit": "F",
            },
        ]
        result = _format_workflows(wfs)
        assert "customworkflow_approve_po" in result
        assert "on create" in result
        assert "on edit" not in result
        assert "Approval workflow" in result

    def test_both_triggers(self):
        wfs = [
            {
                "scriptid": "customworkflow_x",
                "name": "Test WF",
                "recordtype": "salesorder",
                "status": "RELEASED",
                "initoncreate": "T",
                "initonedit": "T",
            },
        ]
        result = _format_workflows(wfs)
        assert "on create" in result
        assert "on edit" in result
