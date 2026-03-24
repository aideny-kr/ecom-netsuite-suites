"""Tests for script type integration in suitescript_sync_service."""


from app.services.suitescript_sync_service import _build_file_path, _sanitize_filename

# ──────────────────────────────────────────────────────────────
# _sanitize_filename
# ──────────────────────────────────────────────────────────────


class TestSanitizeFilename:
    def test_normal_js_file(self):
        assert _sanitize_filename("order_ue.js") == "order_ue.js"

    def test_adds_js_extension(self):
        assert _sanitize_filename("my_script") == "my_script.js"

    def test_strips_special_chars(self):
        assert _sanitize_filename("order@v2!.js") == "order_v2_.js"

    def test_preserves_dots_hyphens_underscores(self):
        assert _sanitize_filename("my-script_v2.0.js") == "my-script_v2.0.js"


# ──────────────────────────────────────────────────────────────
# _build_file_path with script_type
# ──────────────────────────────────────────────────────────────


class TestBuildFilePath:
    def test_with_script_type_user_event(self):
        meta = {"name": "sales_order_ue.js", "source": "file_cabinet"}
        result = _build_file_path(meta, script_type="UserEventScript")
        assert result == "SuiteScripts/User Event Scripts/sales_order_ue.js"

    def test_with_script_type_restlet(self):
        meta = {"name": "api_endpoint.js", "source": "file_cabinet"}
        result = _build_file_path(meta, script_type="Restlet")
        assert result == "SuiteScripts/RESTlets/api_endpoint.js"

    def test_with_script_type_library(self):
        meta = {"name": "date_utils.js", "source": "file_cabinet"}
        result = _build_file_path(meta, script_type="Library")
        assert result == "SuiteScripts/Libraries/date_utils.js"

    def test_with_script_type_other(self):
        meta = {"name": "mystery.js", "source": "file_cabinet"}
        result = _build_file_path(meta, script_type="Other")
        assert result == "SuiteScripts/Other/mystery.js"

    def test_custom_script_with_type(self):
        """Custom scripts get script_id prefix when organized by type."""
        meta = {"name": "my_script.js", "source": "custom_script", "script_id": "customscript_123"}
        result = _build_file_path(meta, script_type="Suitelet")
        assert result == "SuiteScripts/Suitelets/customscript_123_my_script.js"

    def test_without_script_type_file_cabinet(self):
        """Legacy path: no script_type uses original folder structure."""
        meta = {"name": "order.js", "source": "file_cabinet", "folder_path": "ecom"}
        result = _build_file_path(meta, script_type=None)
        assert result == "SuiteScripts/ecom/order.js"

    def test_without_script_type_custom_script(self):
        """Legacy path: custom scripts without type go to CustomScripts/."""
        meta = {"name": "my.js", "source": "custom_script", "script_id": "cs_1"}
        result = _build_file_path(meta, script_type=None)
        assert result == "CustomScripts/cs_1_my.js"

    def test_all_script_types_produce_valid_paths(self):
        """Every canonical type produces a valid path under SuiteScripts/."""
        from app.services.script_type_detector import SCRIPT_TYPES

        meta = {"name": "test.js", "source": "file_cabinet"}
        for stype in SCRIPT_TYPES:
            path = _build_file_path(meta, script_type=stype)
            assert path.startswith("SuiteScripts/"), f"{stype} → {path}"
            assert path.endswith("test.js")

    def test_missing_name_defaults(self):
        meta = {"source": "file_cabinet"}
        result = _build_file_path(meta, script_type="Restlet")
        assert result == "SuiteScripts/RESTlets/unknown.js"
