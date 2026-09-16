"""Subprocess for the local-only group-dispatch crash regression. No live tools."""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.services.audit_service import log_event
from app.services.transaction_ops import accounting_dispatch as dispatch


async def main():
    tenant, parent, ready, mode = UUID(sys.argv[1]), UUID(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    assert urlsplit(url).hostname in {"localhost", "127.0.0.1", "postgres", "db"}
    engine = create_async_engine(url, pool_size=12, max_overflow=5, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gate = asyncio.Lock()

    async def simulated_native(db, tenant_id, parent_id, auth, member):
        async with gate:
            if not ready.exists():
                if mode == "after_write":
                    child = await dispatch.message(db, tenant_id, UUID(member["confirmation_id"]))
                    child.structured_output = {
                        **child.structured_output,
                        "status": "executing",
                        "accounting_execution": {"approved_by": auth["actor_id"], "receipt": None},
                    }
                    await log_event(
                        db,
                        tenant_id,
                        "transaction_ops",
                        "test.only.simulated_native_write",
                        resource_type="chat_message",
                        resource_id=member["confirmation_id"],
                        payload={"native_netsuite_write": False},
                    )
                    await db.commit()
                ready.write_text(json.dumps({"confirmation_id": member["confirmation_id"]}))
        await asyncio.Event().wait()

    dispatch.invoke_child = simulated_native
    try:
        async with factory() as db:
            await dispatch.run_slice(db, tenant, parent)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
