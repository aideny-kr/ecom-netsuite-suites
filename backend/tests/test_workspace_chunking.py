"""Tests for workspace script chunking — preamble capture (workspace-chunking-fix).

The RAG seeder must capture code before the first entry point (constants,
config objects, helper functions) as a "preamble" chunk. Without this,
business-critical content like item replacement rules is invisible to rag_search.
"""

from app.services.workspace_rag_seeder import (
    _chunk_by_entry_points,
    _find_entry_points,
)


# ── Sample SuiteScript with constants before entry points ──

SCRIPT_WITH_PREAMBLE = """\
/**
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 */
define(['N/record', 'N/log'], (record, log) => {

    // CR replacement rule (FRANCR000B -> FRANCR000Z) + add FRANBVTK0B
    const CR_ITEMS_TO_REPLACE = {
        1759: 1763  // 1759 (FRANCR000B) -> 1763 (FRANCR000Z)
    };
    const CR_COMPONENTS = [2623]; // FRANBVTK0B

    const CARRY_LINE_FIELDS = ['class', 'department', 'location'];

    function hasPanurgyLocation(rec) {
        const lineCount = rec.getLineCount({ sublistId: 'item' });
        for (let i = 0; i < lineCount; i++) {
            const loc = rec.getSublistText({ sublistId: 'item', fieldId: 'location', line: i });
            if (loc && loc.toUpperCase().includes('PANURGY')) return true;
        }
        return false;
    }

    const beforeSubmit = (context) => {
        if (context.type !== context.UserEventType.CREATE) return;
        const newRecord = context.newRecord;
        if (hasPanurgyLocation(newRecord)) {
            replaceAndAddComponentLines(newRecord, CARRY_LINE_FIELDS, CR_ITEMS_TO_REPLACE, CR_COMPONENTS);
        }
    };

    const afterSubmit = (context) => {
        log.audit('afterSubmit', 'completed');
    };

    return { beforeSubmit, afterSubmit };
});
"""

SCRIPT_NO_PREAMBLE = """\
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 */
define(['N/log'], (log) => {
    const get = (params) => {
        return { success: true };
    };
    return { get };
});
"""

SCRIPT_ONLY_CONSTANTS = """\
/**
 * Constants file — no entry points
 */
const ITEM_MAP = { 1759: 1763 };
const LOCATIONS = ['Panurgy', 'HQ'];
"""


class TestPreambleChunkCapture:
    """Code before the first entry point should be captured as a #preamble chunk."""

    def test_preamble_chunk_created(self):
        """File with constants before first entry point → preamble chunk exists."""
        chunks = _chunk_by_entry_points(SCRIPT_WITH_PREAMBLE, "test.js", "UserEventScript")
        chunk_names = [c[1] for c in chunks]
        assert any("#preamble" in name for name in chunk_names), (
            f"No preamble chunk found. Chunks: {chunk_names}"
        )

    def test_preamble_contains_constants(self):
        """The preamble chunk should include variable declarations and config."""
        chunks = _chunk_by_entry_points(SCRIPT_WITH_PREAMBLE, "test.js", "UserEventScript")
        preamble_chunks = [c for c in chunks if "#preamble" in c[1]]
        assert len(preamble_chunks) == 1
        content = preamble_chunks[0][2]
        assert "CR_ITEMS_TO_REPLACE" in content
        assert "FRANCR000B" in content
        assert "1759" in content

    def test_preamble_contains_helper_functions(self):
        """Helper functions before the first entry point should be in preamble."""
        chunks = _chunk_by_entry_points(SCRIPT_WITH_PREAMBLE, "test.js", "UserEventScript")
        preamble_chunks = [c for c in chunks if "#preamble" in c[1]]
        content = preamble_chunks[0][2]
        assert "hasPanurgyLocation" in content

    def test_entry_points_still_chunked(self):
        """Entry point chunks should still exist alongside preamble."""
        chunks = _chunk_by_entry_points(SCRIPT_WITH_PREAMBLE, "test.js", "UserEventScript")
        chunk_names = [c[1] for c in chunks]
        assert any("#beforeSubmit" in name for name in chunk_names)
        assert any("#afterSubmit" in name for name in chunk_names)

    def test_no_preamble_for_short_prefix(self):
        """Files where first entry point is near the top → no preamble chunk."""
        chunks = _chunk_by_entry_points(SCRIPT_NO_PREAMBLE, "restlet.js", "Restlet")
        chunk_names = [c[1] for c in chunks]
        assert not any("#preamble" in name for name in chunk_names), (
            f"Unexpected preamble chunk for short prefix. Chunks: {chunk_names}"
        )

    def test_no_entry_points_whole_file(self):
        """Files with no entry points → single whole-file chunk (existing behavior)."""
        chunks = _chunk_by_entry_points(SCRIPT_ONLY_CONSTANTS, "constants.js", None)
        assert len(chunks) == 1
        content = chunks[0][2]
        assert "ITEM_MAP" in content
        assert "LOCATIONS" in content

    def test_preamble_has_context_header(self):
        """Preamble chunk should have file path and script type in header."""
        chunks = _chunk_by_entry_points(SCRIPT_WITH_PREAMBLE, "SuiteScripts/test.js", "UserEventScript")
        preamble_chunks = [c for c in chunks if "#preamble" in c[1]]
        content = preamble_chunks[0][2]
        assert "// File: SuiteScripts/test.js" in content
        assert "// Script Type: UserEventScript" in content

    def test_chunk_count_correct(self):
        """File with preamble + 2 entry points → 3 chunks total."""
        chunks = _chunk_by_entry_points(SCRIPT_WITH_PREAMBLE, "test.js", "UserEventScript")
        # preamble + beforeSubmit + afterSubmit = 3
        assert len(chunks) == 3


class TestFindEntryPoints:
    """Verify entry point detection still works correctly."""

    def test_finds_beforesubmit_and_aftersubmit(self):
        eps = _find_entry_points(SCRIPT_WITH_PREAMBLE)
        names = [name for name, _ in eps]
        assert "beforeSubmit" in names
        assert "afterSubmit" in names

    def test_no_entry_points_in_constants_file(self):
        eps = _find_entry_points(SCRIPT_ONLY_CONSTANTS)
        assert eps == []
