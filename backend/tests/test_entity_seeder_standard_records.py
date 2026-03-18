"""TDD: Entity seeder must seed standard records (locations, subsidiaries,
departments, classes) into tenant_entity_mapping — not just custom fields.

Without these, the entity resolver can't fuzzy-match location names like
"Panurgy" and falls back to custom field matches like custbody_fw_sent_to_panurgy.
"""

import uuid
from unittest.mock import MagicMock

from app.services.tenant_entity_seeder import _build_rows


def _make_metadata(**kwargs):
    meta = MagicMock()
    meta.custom_record_types = kwargs.get("custom_record_types", [])
    meta.transaction_body_fields = kwargs.get("transaction_body_fields", [])
    meta.transaction_column_fields = kwargs.get("transaction_column_fields", [])
    meta.entity_custom_fields = kwargs.get("entity_custom_fields", [])
    meta.item_custom_fields = kwargs.get("item_custom_fields", [])
    meta.custom_record_fields = kwargs.get("custom_record_fields", [])
    meta.custom_lists = kwargs.get("custom_lists", [])
    meta.custom_list_values = kwargs.get("custom_list_values", {})
    meta.saved_searches = kwargs.get("saved_searches", [])
    meta.scripts = kwargs.get("scripts", [])
    meta.script_deployments = kwargs.get("script_deployments", [])
    meta.workflows = kwargs.get("workflows", [])
    meta.locations = kwargs.get("locations", [])
    meta.subsidiaries = kwargs.get("subsidiaries", [])
    meta.departments = kwargs.get("departments", [])
    meta.classifications = kwargs.get("classifications", [])
    return meta


class TestLocationSeeding:
    def test_locations_are_seeded(self):
        """Location names must be seeded so 'Panurgy' resolves to a location, not a custom field."""
        meta = _make_metadata(locations=[
            {"id": 69, "name": "Panurgy"},
            {"id": 20, "name": "Compal"},
            {"id": 25, "name": "Goods In"},
        ])
        rows = _build_rows(uuid.uuid4(), meta)
        location_rows = [r for r in rows if r["entity_type"] == "location"]
        assert len(location_rows) == 3
        panurgy = next(r for r in location_rows if "Panurgy" in r["natural_name"])
        assert panurgy["script_id"] == "69"
        assert panurgy["entity_type"] == "location"

    def test_location_with_parent(self):
        """Locations with parent (sublocation) should include full name."""
        meta = _make_metadata(locations=[
            {"id": 35, "name": "Consumables", "parent": "Dimerco"},
        ])
        rows = _build_rows(uuid.uuid4(), meta)
        location_rows = [r for r in rows if r["entity_type"] == "location"]
        assert len(location_rows) == 1
        assert "Consumables" in location_rows[0]["natural_name"]


class TestSubsidiarySeeding:
    def test_subsidiaries_are_seeded(self):
        meta = _make_metadata(subsidiaries=[
            {"id": 1, "name": "Framework Computer Inc"},
            {"id": 5, "name": "Framework Computer GmbH"},
        ])
        rows = _build_rows(uuid.uuid4(), meta)
        sub_rows = [r for r in rows if r["entity_type"] == "subsidiary"]
        assert len(sub_rows) == 2
        assert sub_rows[0]["script_id"] == "1"


class TestDepartmentSeeding:
    def test_departments_are_seeded(self):
        meta = _make_metadata(departments=[
            {"id": 3, "name": "Engineering"},
            {"id": 7, "name": "Operations"},
        ])
        rows = _build_rows(uuid.uuid4(), meta)
        dept_rows = [r for r in rows if r["entity_type"] == "department"]
        assert len(dept_rows) == 2


class TestClassificationSeeding:
    def test_classifications_are_seeded(self):
        meta = _make_metadata(classifications=[
            {"id": 1, "name": "Laptop"},
            {"id": 2, "name": "Accessory"},
        ])
        rows = _build_rows(uuid.uuid4(), meta)
        class_rows = [r for r in rows if r["entity_type"] == "classification"]
        assert len(class_rows) == 2


class TestEmptyStandardRecords:
    def test_empty_locations_no_crash(self):
        meta = _make_metadata(locations=[])
        rows = _build_rows(uuid.uuid4(), meta)
        assert [r for r in rows if r["entity_type"] == "location"] == []

    def test_none_locations_no_crash(self):
        meta = _make_metadata(locations=None)
        rows = _build_rows(uuid.uuid4(), meta)
        assert [r for r in rows if r["entity_type"] == "location"] == []
