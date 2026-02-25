"""Tests for workspace_rag_seeder — chunking SuiteScript source code for RAG."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import DocChunk
from app.models.workspace import Workspace, WorkspaceFile
from app.services.workspace_rag_seeder import (
    _chunk_by_entry_points,
    _detect_script_type,
    _find_entry_points,
    seed_workspace_scripts,
)
from tests.conftest import create_test_tenant, create_test_user

# ──────────────────────────────────────────────────────────────────
# Sample SuiteScript content for tests
# ──────────────────────────────────────────────────────────────────

USER_EVENT_SCRIPT = """\
/**
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 */
define(['N/record', 'N/log'], (record, log) => {
    const beforeSubmit = (ctx) => {
        log.debug('beforeSubmit', 'running');
        const rec = ctx.newRecord;
        rec.setValue('custbody_processed', true);
    };

    const afterSubmit = (ctx) => {
        log.audit('afterSubmit', 'completed');
    };

    return { beforeSubmit, afterSubmit };
});
"""

MAP_REDUCE_SCRIPT = """\
/**
 * @NApiVersion 2.1
 * @NScriptType MapReduceScript
 */
define(['N/search', 'N/record'], (search, record) => {
    function getInputData() {
        return search.create({ type: 'salesorder', filters: [] });
    }

    function map(ctx) {
        const data = JSON.parse(ctx.value);
        ctx.write(data.id, data);
    }

    function reduce(ctx) {
        const values = ctx.values.map(JSON.parse);
        log.debug('reduce', values.length);
    }

    function summarize(summary) {
        log.audit('Done', summary.inputSummary);
    }

    return { getInputData, map, reduce, summarize };
});
"""

RESTLET_SCRIPT = """\
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 */
define(['N/record'], (record) => {
    const get = (requestParams) => {
        return { success: true };
    };

    const post = (requestBody) => {
        return { success: true };
    };

    return { get, post };
});
"""

PLAIN_JS = """\
// A utility module with no entry points
function formatCurrency(amount) {
    return '$' + amount.toFixed(2);
}

function parseDate(str) {
    return new Date(str);
}
"""

OBJECT_LITERAL_SCRIPT = """\
/**
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 */
define(['N/log'], (log) => {
    return {
        beforeLoad: function(ctx) {
            log.debug('loaded');
        },
        beforeSubmit: function(ctx) {
            log.debug('submitting');
        }
    };
});
"""


# ──────────────────────────────────────────────────────────────────
# Unit Tests — Pure functions, no DB
# ──────────────────────────────────────────────────────────────────


class TestDetectScriptType:
    def test_user_event(self):
        assert _detect_script_type(USER_EVENT_SCRIPT) == "UserEventScript"

    def test_map_reduce(self):
        assert _detect_script_type(MAP_REDUCE_SCRIPT) == "MapReduceScript"

    def test_restlet(self):
        assert _detect_script_type(RESTLET_SCRIPT) == "Restlet"

    def test_no_annotation(self):
        assert _detect_script_type(PLAIN_JS) is None

    def test_suitelet(self):
        content = "/** @NScriptType Suitelet */\ndefine([], () => {});"
        assert _detect_script_type(content) == "Suitelet"


class TestFindEntryPoints:
    def test_user_event_entry_points(self):
        eps = _find_entry_points(USER_EVENT_SCRIPT)
        names = [name for name, _ in eps]
        assert names == ["beforeSubmit", "afterSubmit"]

    def test_map_reduce_entry_points(self):
        eps = _find_entry_points(MAP_REDUCE_SCRIPT)
        names = [name for name, _ in eps]
        assert names == ["getInputData", "map", "reduce", "summarize"]

    def test_restlet_entry_points(self):
        eps = _find_entry_points(RESTLET_SCRIPT)
        names = [name for name, _ in eps]
        assert names == ["get", "post"]

    def test_plain_js_no_entry_points(self):
        eps = _find_entry_points(PLAIN_JS)
        assert eps == []

    def test_object_literal_pattern(self):
        eps = _find_entry_points(OBJECT_LITERAL_SCRIPT)
        names = [name for name, _ in eps]
        assert "beforeLoad" in names
        assert "beforeSubmit" in names

    def test_offsets_are_sorted(self):
        eps = _find_entry_points(MAP_REDUCE_SCRIPT)
        offsets = [offset for _, offset in eps]
        assert offsets == sorted(offsets)

    def test_no_duplicates(self):
        # Script that mentions beforeSubmit twice (definition + return)
        content = """\
const beforeSubmit = (ctx) => { ctx.newRecord; };
const afterSubmit = (ctx) => { };
return { beforeSubmit, afterSubmit };
"""
        eps = _find_entry_points(content)
        names = [name for name, _ in eps]
        assert names.count("beforeSubmit") == 1
        assert names.count("afterSubmit") == 1


class TestChunkByEntryPoints:
    def test_user_event_produces_two_chunks(self):
        chunks = _chunk_by_entry_points(USER_EVENT_SCRIPT, "ue/order.js", "UserEventScript")
        assert len(chunks) == 2
        titles = [t for t, _, _ in chunks]
        assert "ue/order.js#beforeSubmit" in titles
        assert "ue/order.js#afterSubmit" in titles

    def test_map_reduce_produces_four_chunks(self):
        chunks = _chunk_by_entry_points(MAP_REDUCE_SCRIPT, "mr/sync.js", "MapReduceScript")
        assert len(chunks) == 4

    def test_plain_js_single_chunk(self):
        chunks = _chunk_by_entry_points(PLAIN_JS, "utils/format.js", None)
        assert len(chunks) == 1
        title, source, content = chunks[0]
        assert title == "utils/format.js"
        assert "# Entry Point" not in content

    def test_chunk_header_includes_metadata(self):
        chunks = _chunk_by_entry_points(USER_EVENT_SCRIPT, "ue/order.js", "UserEventScript")
        _, _, content = chunks[0]
        assert "// File: ue/order.js" in content
        assert "// Script Type: UserEventScript" in content
        assert "// Entry Point: beforeSubmit" in content

    def test_chunk_without_script_type(self):
        chunks = _chunk_by_entry_points(RESTLET_SCRIPT, "rest/api.js", None)
        _, _, content = chunks[0]
        assert "// File: rest/api.js" in content
        assert "Script Type" not in content

    def test_large_chunk_truncated(self):
        # Create content larger than 6000 chars
        big_content = "const execute = () => {\n" + ("x = 1;\n" * 1500) + "};"
        chunks = _chunk_by_entry_points(big_content, "big.js", "ScheduledScript")
        _, _, content = chunks[0]
        assert content.endswith("// ... (truncated)")
        assert len(content) < 7000


# ──────────────────────────────────────────────────────────────────
# Integration Tests — Requires DB
# ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def workspace_setup(db: AsyncSession):
    """Create a tenant, user, workspace, and sample .js files."""
    tenant = await create_test_tenant(db, name="RAG Test Corp")
    user, _ = await create_test_user(db, tenant)

    ws = Workspace(
        tenant_id=tenant.id,
        name="Test Workspace",
        status="active",
        created_by=user.id,
    )
    db.add(ws)
    await db.flush()

    # Add a UserEvent script file
    ue_file = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=ws.id,
        path="ue/WD_UE_Orders.js",
        file_name="WD_UE_Orders.js",
        content=USER_EVENT_SCRIPT,
        sha256_hash="abc123",
        is_directory=False,
        size_bytes=len(USER_EVENT_SCRIPT),
    )
    db.add(ue_file)

    # Add a MapReduce script file
    mr_file = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=ws.id,
        path="mr/WD_MR_Sync.js",
        file_name="WD_MR_Sync.js",
        content=MAP_REDUCE_SCRIPT,
        sha256_hash="def456",
        is_directory=False,
        size_bytes=len(MAP_REDUCE_SCRIPT),
    )
    db.add(mr_file)

    # Add a plain utility JS file
    util_file = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=ws.id,
        path="utils/format.js",
        file_name="format.js",
        content=PLAIN_JS,
        sha256_hash="ghi789",
        is_directory=False,
        size_bytes=len(PLAIN_JS),
    )
    db.add(util_file)

    # Add a directory (should be skipped)
    dir_entry = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=ws.id,
        path="ue/",
        file_name="ue",
        is_directory=True,
        size_bytes=0,
    )
    db.add(dir_entry)

    # Add a non-JS file (should be skipped)
    txt_file = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=ws.id,
        path="README.md",
        file_name="README.md",
        content="# Readme",
        sha256_hash="txt000",
        is_directory=False,
        size_bytes=8,
    )
    db.add(txt_file)

    await db.flush()
    return tenant, ws


@pytest.mark.asyncio
async def test_seeds_chunks_for_js_files(db: AsyncSession, workspace_setup):
    """Seeding creates DocChunks with correct source paths and metadata."""
    tenant, ws = workspace_setup

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)

    # UE: 2 entry points, MR: 4 entry points, util: 1 whole file = 7
    assert count == 7

    result = await db.execute(
        select(DocChunk).where(
            DocChunk.tenant_id == tenant.id,
            DocChunk.source_path.like("workspace_scripts/%"),
        )
    )
    chunks = result.scalars().all()
    assert len(chunks) == 7

    # Check source paths
    paths = {c.source_path for c in chunks}
    assert "workspace_scripts/ue/WD_UE_Orders.js#beforeSubmit" in paths
    assert "workspace_scripts/ue/WD_UE_Orders.js#afterSubmit" in paths
    assert "workspace_scripts/mr/WD_MR_Sync.js#getInputData" in paths
    assert "workspace_scripts/utils/format.js" in paths

    # Check metadata
    ue_chunk = [c for c in chunks if c.source_path.endswith("#beforeSubmit")][0]
    assert ue_chunk.metadata_["type"] == "workspace_script"
    assert ue_chunk.metadata_["sha256_hash"] == "abc123"
    assert ue_chunk.metadata_["script_type"] == "UserEventScript"
    assert ue_chunk.metadata_["entry_point"] == "beforeSubmit"


@pytest.mark.asyncio
async def test_incremental_skips_unchanged(db: AsyncSession, workspace_setup):
    """Second seed with same hashes returns 0 new chunks."""
    tenant, ws = workspace_setup

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        first_count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)
        assert first_count == 7

        second_count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)
        assert second_count == 0


@pytest.mark.asyncio
async def test_incremental_reprocesses_changed_file(db: AsyncSession, workspace_setup):
    """Changing a file's hash triggers re-chunking for that file only."""
    tenant, ws = workspace_setup

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)

        # Change the hash of one file
        file_result = await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == ws.id,
                WorkspaceFile.path == "ue/WD_UE_Orders.js",
            )
        )
        ue_file = file_result.scalar_one()
        ue_file.sha256_hash = "changed_hash"
        await db.flush()

        second_count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)
        # Only the UE file re-chunked: 2 entry points
        assert second_count == 2


@pytest.mark.asyncio
async def test_force_reseeds_all(db: AsyncSession, workspace_setup):
    """force=True re-processes all files regardless of hash."""
    tenant, ws = workspace_setup

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        first_count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)
        assert first_count == 7

        force_count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id, force=True)
        assert force_count == 7

    # Verify no duplicates
    result = await db.execute(
        select(DocChunk).where(
            DocChunk.tenant_id == tenant.id,
            DocChunk.source_path.like("workspace_scripts/%"),
        )
    )
    chunks = result.scalars().all()
    assert len(chunks) == 7


@pytest.mark.asyncio
async def test_skips_directories_and_non_js(db: AsyncSession, workspace_setup):
    """Directories and non-.js files are not chunked."""
    tenant, ws = workspace_setup

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)

    result = await db.execute(
        select(DocChunk).where(
            DocChunk.tenant_id == tenant.id,
            DocChunk.source_path.like("workspace_scripts/%"),
        )
    )
    chunks = result.scalars().all()
    paths = {c.source_path for c in chunks}
    # No README or directory entries
    assert not any("README" in p for p in paths)
    assert not any(p.endswith("/") for p in paths)


@pytest.mark.asyncio
async def test_idempotency(db: AsyncSession, workspace_setup):
    """Two force runs produce same count, no duplicates."""
    tenant, ws = workspace_setup

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        c1 = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id, force=True)
        c2 = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id, force=True)

    assert c1 == c2

    result = await db.execute(
        select(DocChunk).where(
            DocChunk.tenant_id == tenant.id,
            DocChunk.source_path.like("workspace_scripts/%"),
        )
    )
    assert len(result.scalars().all()) == c1


@pytest.mark.asyncio
async def test_tenant_isolation(db: AsyncSession, workspace_setup):
    """Tenant A chunks are invisible to tenant B seed operation."""
    tenant_a, ws_a = workspace_setup

    # Seed tenant A
    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        await seed_workspace_scripts(db, tenant_a.id, workspace_id=ws_a.id)

    # Create tenant B with no workspace
    tenant_b = await create_test_tenant(db, name="Tenant B")

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        b_count = await seed_workspace_scripts(db, tenant_b.id)

    assert b_count == 0

    # Verify tenant A chunks still exist
    result = await db.execute(
        select(DocChunk).where(
            DocChunk.tenant_id == tenant_a.id,
            DocChunk.source_path.like("workspace_scripts/%"),
        )
    )
    assert len(result.scalars().all()) == 7

    # Verify tenant B has no chunks
    result = await db.execute(
        select(DocChunk).where(
            DocChunk.tenant_id == tenant_b.id,
            DocChunk.source_path.like("workspace_scripts/%"),
        )
    )
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_no_workspace_returns_zero(db: AsyncSession):
    """Tenant with no workspace returns 0."""
    tenant = await create_test_tenant(db, name="Empty Tenant")
    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        count = await seed_workspace_scripts(db, tenant.id)
    assert count == 0


@pytest.mark.asyncio
async def test_empty_content_skipped(db: AsyncSession, workspace_setup):
    """Files with empty/null content are skipped."""
    tenant, ws = workspace_setup

    # Add file with no content
    empty_file = WorkspaceFile(
        tenant_id=tenant.id,
        workspace_id=ws.id,
        path="empty.js",
        file_name="empty.js",
        content=None,
        sha256_hash="empty000",
        is_directory=False,
        size_bytes=0,
    )
    db.add(empty_file)
    await db.flush()

    with patch("app.services.workspace_rag_seeder.embed_texts", new_callable=AsyncMock, return_value=None):
        count = await seed_workspace_scripts(db, tenant.id, workspace_id=ws.id)

    # Should still be 7 (3 files with content), not 8
    assert count == 7
