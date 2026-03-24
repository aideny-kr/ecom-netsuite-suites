"""Tests for script_type_detector — shared SuiteScript type detection."""

import pytest

from app.services.script_type_detector import (
    SCRIPT_TYPE_FOLDER_MAP,
    SCRIPT_TYPES,
    detect_from_content,
    detect_from_filename,
    get_folder_for_type,
    resolve_script_type,
)

# ──────────────────────────────────────────────────────────────
# detect_from_content
# ──────────────────────────────────────────────────────────────


class TestDetectFromContent:
    @pytest.mark.parametrize(
        "annotation, expected",
        [
            ("UserEventScript", "UserEventScript"),
            ("ClientScript", "ClientScript"),
            ("ScheduledScript", "ScheduledScript"),
            ("MapReduceScript", "MapReduceScript"),
            ("Suitelet", "Suitelet"),
            ("Restlet", "Restlet"),
            ("WorkflowActionScript", "WorkflowActionScript"),
            ("BundleInstallationScript", "BundleInstallationScript"),
            ("MassUpdateScript", "MassUpdateScript"),
        ],
    )
    def test_all_annotation_types(self, annotation: str, expected: str):
        content = f"""/**
 * @NApiVersion 2.1
 * @NScriptType {annotation}
 */
define([], function() {{ }});"""
        assert detect_from_content(content) == expected

    def test_case_insensitive(self):
        content = "/** @NScriptType usereventscript */"
        assert detect_from_content(content) == "UserEventScript"

    def test_no_annotation(self):
        content = "define(['N/record'], function(record) { });"
        assert detect_from_content(content) is None

    def test_empty_content(self):
        assert detect_from_content("") is None

    def test_library_not_in_annotations(self):
        """Library is a filename heuristic, not an @NScriptType value."""
        content = "/** @NScriptType Library */"
        assert detect_from_content(content) is None

    def test_annotation_with_extra_whitespace(self):
        content = "/** @NScriptType   Restlet  */"
        assert detect_from_content(content) == "Restlet"


# ──────────────────────────────────────────────────────────────
# detect_from_filename
# ──────────────────────────────────────────────────────────────


class TestDetectFromFilename:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("ecom_sales_order_ue.js", "UserEventScript"),
            ("item_fulfillment_userevent.js", "UserEventScript"),
            ("sales_order_client.js", "ClientScript"),
            ("po_form_cs.js", "ClientScript"),
            ("daily_sync_scheduled.js", "ScheduledScript"),
            ("nightly_cleanup_ss.js", "ScheduledScript"),
            ("order_processing_mapreduce.js", "MapReduceScript"),
            ("bulk_update_mr.js", "MapReduceScript"),
            ("item_lookup_suitelet.js", "Suitelet"),
            ("dashboard_su.js", "Suitelet"),
            ("api_endpoint_restlet.js", "Restlet"),
            ("external_integration_rl.js", "Restlet"),
            ("approval_workflow.js", "WorkflowActionScript"),
            ("review_step_wa.js", "WorkflowActionScript"),
            ("install_bundle.js", "BundleInstallationScript"),
            ("setup_bi.js", "BundleInstallationScript"),
            ("update_prices_massupdate.js", "MassUpdateScript"),
            ("fix_records_mu.js", "MassUpdateScript"),
            ("date_utils.js", "Library"),
            ("search_lib.js", "Library"),
            ("format_helper.js", "Library"),
        ],
    )
    def test_all_filename_patterns(self, filename: str, expected: str):
        assert detect_from_filename(filename) == expected

    def test_no_match(self):
        assert detect_from_filename("random_script.js") is None

    def test_case_insensitive(self):
        assert detect_from_filename("OrderProcessing_UE.js") == "UserEventScript"

    def test_path_with_directories(self):
        assert detect_from_filename("SuiteScripts/ecom/sales_order_ue.js") == "UserEventScript"


# ──────────────────────────────────────────────────────────────
# resolve_script_type (priority chain)
# ──────────────────────────────────────────────────────────────


class TestResolveScriptType:
    def test_content_wins_over_filename(self):
        """@NScriptType in content takes priority over filename heuristic."""
        content = "/** @NScriptType Restlet */"
        result = resolve_script_type(content=content, filename="some_ue_script.js")
        assert result == "Restlet"

    def test_metadata_wins_over_filename(self):
        result = resolve_script_type(
            content=None, filename="random.js", metadata_type="Suitelet"
        )
        assert result == "Suitelet"

    def test_content_wins_over_metadata(self):
        content = "/** @NScriptType ClientScript */"
        result = resolve_script_type(
            content=content, filename="x.js", metadata_type="Restlet"
        )
        assert result == "ClientScript"

    def test_filename_fallback(self):
        result = resolve_script_type(content=None, filename="order_ue.js")
        assert result == "UserEventScript"

    def test_all_none_returns_other(self):
        result = resolve_script_type(content=None, filename="mystery.js")
        assert result == "Other"

    def test_empty_content_falls_to_filename(self):
        result = resolve_script_type(content="", filename="sync_scheduled.js")
        assert result == "ScheduledScript"

    def test_never_returns_none(self):
        """resolve_script_type always returns a string, never None."""
        result = resolve_script_type()
        assert result == "Other"
        assert isinstance(result, str)


# ──────────────────────────────────────────────────────────────
# Folder mapping
# ──────────────────────────────────────────────────────────────


class TestFolderMapping:
    def test_all_types_have_folders(self):
        for t in SCRIPT_TYPES:
            assert t in SCRIPT_TYPE_FOLDER_MAP

    def test_get_folder_for_known_type(self):
        assert get_folder_for_type("UserEventScript") == "User Event Scripts"
        assert get_folder_for_type("Restlet") == "RESTlets"
        assert get_folder_for_type("Library") == "Libraries"

    def test_get_folder_for_unknown_type(self):
        assert get_folder_for_type("SomeNewType") == "Other"

    def test_folder_names_unique(self):
        folders = list(SCRIPT_TYPE_FOLDER_MAP.values())
        assert len(folders) == len(set(folders))
