import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from app.models.drive import DriveChunk, DriveFile, DriveFolder
from app.services.drive_rag.retriever import retrieve_drive_chunks
from tests.conftest import create_test_tenant, create_test_user


@pytest.mark.asyncio
async def test_retrieve_returns_empty_when_no_chunks(db):
    tenant = await create_test_tenant(db)
    with patch(
        "app.services.drive_rag.retriever.embed_query",
        new=AsyncMock(return_value=[0.0] * 1024),
    ):
        chunks = await retrieve_drive_chunks(db, tenant_id=tenant.id, query_text="anything")
    assert chunks == []


@pytest.mark.asyncio
async def test_retrieve_is_tenant_scoped(db):
    t1 = await create_test_tenant(db, name="T1")
    t2 = await create_test_tenant(db, name="T2")
    user1, _ = await create_test_user(db, t1)
    folder = DriveFolder(tenant_id=t1.id, folder_id="FID", folder_name="F", created_by=user1.id)
    db.add(folder)
    await db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    file = DriveFile(
        tenant_id=t1.id, folder_id=folder.id, drive_file_id="f1", name="Docs",
        mime_type="text/plain", web_view_link="https://x",
        modified_time=now, indexed_at=now, chunk_count=1,
    )
    db.add(file)
    await db.flush()
    db.add(
        DriveChunk(
            tenant_id=t1.id, file_id=file.id, chunk_index=0,
            content="tenant 1 content", token_count=5,
            embedding=[0.1] * 1024,
            metadata_={"source_name": "Docs", "web_view_link": "https://x"},
        )
    )
    await db.commit()

    with patch(
        "app.services.drive_rag.retriever.embed_query",
        new=AsyncMock(return_value=[0.1] * 1024),
    ):
        t1_chunks = await retrieve_drive_chunks(db, tenant_id=t1.id, query_text="q")
        t2_chunks = await retrieve_drive_chunks(db, tenant_id=t2.id, query_text="q")

    assert len(t1_chunks) == 1
    assert t1_chunks[0]["content"] == "tenant 1 content"
    assert t1_chunks[0]["source_name"] == "Docs"
    assert t1_chunks[0]["web_view_link"] == "https://x"
    assert t2_chunks == []


@pytest.mark.asyncio
async def test_retrieve_returns_empty_when_embedding_unavailable(db):
    tenant = await create_test_tenant(db)
    with patch(
        "app.services.drive_rag.retriever.embed_query",
        new=AsyncMock(return_value=None),
    ):
        chunks = await retrieve_drive_chunks(db, tenant_id=tenant.id, query_text="q")
    assert chunks == []


@pytest.mark.asyncio
async def test_retrieve_skips_null_embedding(db):
    """Chunks without embeddings (graceful degradation mode) must be excluded."""
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="FID", folder_name="F", created_by=user.id)
    db.add(folder)
    await db.flush()
    now = dt.datetime.now(dt.timezone.utc)
    file = DriveFile(
        tenant_id=tenant.id, folder_id=folder.id, drive_file_id="f1", name="Docs",
        mime_type="text/plain", web_view_link="https://x",
        modified_time=now, indexed_at=now, chunk_count=1,
    )
    db.add(file)
    await db.flush()
    db.add(
        DriveChunk(
            tenant_id=tenant.id, file_id=file.id, chunk_index=0,
            content="no embedding", token_count=5,
            embedding=None,
            metadata_={"source_name": "Docs", "web_view_link": "https://x"},
        )
    )
    await db.commit()

    with patch(
        "app.services.drive_rag.retriever.embed_query",
        new=AsyncMock(return_value=[0.1] * 1024),
    ):
        chunks = await retrieve_drive_chunks(db, tenant_id=tenant.id, query_text="q")
    assert chunks == []
