from sqlalchemy import func, select

from app.services.ingestion import solidus_sync as sync
from tests.test_solidus_ingestion import NOW, connection, fake_pages, source_order


async def test_competing_imports_serialize_and_resume_after_lock_release(db, admin_user, monkeypatch):
    tenant = admin_user[0].tenant_id
    source = await connection(db, tenant)
    source_id = source.id
    await db.commit()
    calls = fake_pages(monkeypatch, [source_order()])
    key = f"solidus_orders:{tenant}:{source_id}"
    async with db.bind.engine.connect() as competing:
        await competing.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
        result = await sync.sync_solidus_orders(db, tenant, source_id, now=NOW)
        assert result["reason"] == "refresh_in_progress"
        assert calls == []
    result = await sync.sync_solidus_orders(db, tenant, source_id, now=NOW)
    assert result["complete"] is True
    assert len(calls) == 1
