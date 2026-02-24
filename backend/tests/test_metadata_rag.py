"""Tests for netsuite_metadata_rag formatters."""

from app.services.netsuite_metadata_rag import _format_custom_list_values, _format_saved_searches


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
