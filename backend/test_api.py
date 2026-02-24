import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import select

from app.core.database import worker_async_session
from app.models.connection import Connection
from app.models.workspace import WorkspaceFile
from app.services.suitescript_sync_service import sync_scripts_to_workspace


async def main():
    async with worker_async_session() as db:
        result = await db.execute(select(Connection).where(Connection.provider == 'netsuite', Connection.id == 'fcde67d0-f94c-4277-be33-fabac28862e2').limit(1))
        connection = result.scalar_one_or_none()
        if not connection:
            print("No connection found")
            return

        import app.services.suitescript_sync_service as ss
        orig_discover = ss.discover_scripts
        import app.services.netsuite_restlet_client as rlc
        orig_read = rlc.restlet_read_file

        async def mock_discover(*args, **kwargs):
            return [{"file_id": "1", "name": "test.js", "folder": "123", "script_type": None, "source": "file_cabinet"}]

        async def mock_read(*args, **kwargs):
            return {"success": True, "content": "function test() { return 1; }"}

        ss.discover_scripts = mock_discover
        rlc.restlet_read_file = mock_read

        try:
            res = await sync_scripts_to_workspace(
                db=db,
                tenant_id=connection.tenant_id,
                connection_id=connection.id,
                access_token="mock",
                account_id="mock",
                user_id=connection.created_by,
            )
            print(f"Sync complete: {res}")

            # check files in workspace
            workspace_id = res["workspace_id"]
            files = await db.execute(select(WorkspaceFile).where(WorkspaceFile.workspace_id == workspace_id))
            file_list = files.scalars().all()
            print(f"Database contains {len(file_list)} files for workspace {workspace_id}")
            for f in file_list:
                print(f"- {f.path}: {f.size_bytes} bytes")

        finally:
            ss.discover_scripts = orig_discover
            rlc.restlet_read_file = orig_read
            await db.commit()

if __name__ == "__main__":
    asyncio.run(main())
