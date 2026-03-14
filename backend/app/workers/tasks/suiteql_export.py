"""Celery task for async SuiteQL CSV export with pagination."""

import asyncio
import csv
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.workers.base_task import InstrumentedTask, tenant_session
from app.workers.celery_app import celery_app

# Export files land here; served via a download endpoint or object storage
EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/tmp/exports"))


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.suiteql_export",
    queue="export",
)
def export_suiteql_to_csv(
    self,
    tenant_id: str,
    query_text: str,
    query_name: str = "export",
    **kwargs,
):
    """Paginate a SuiteQL query in 1000-row chunks and save to CSV.

    Uses OFFSET + FETCH FIRST to walk through all pages.
    Returns a dict with the file path and row count.
    """
    from app.core.encryption import decrypt_credentials
    from app.models.connection import Connection
    from app.services.netsuite_client import execute_suiteql_via_rest

    # Resolve NetSuite credentials synchronously
    with tenant_session(tenant_id) as session:
        from sqlalchemy import select as sa_select

        result = session.execute(
            sa_select(Connection)
            .where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status == "active",
            )
            .limit(1)
        )
        connection = result.scalar_one_or_none()
        if not connection:
            raise ValueError("No active NetSuite connection found.")

        creds = decrypt_credentials(connection.encrypted_credentials)
        account_id = creds.get("account_id", "") or creds.get("netsuite_account_id", "")
        if not account_id:
            raise ValueError("Connection missing account_id.")

        # For sync tasks, get the current access token directly
        access_token = creds.get("access_token", "")
        if not access_token:
            raise ValueError("No access token available. Please re-authorize.")

    # Strip trailing FETCH FIRST to avoid double-up (but preserve subquery FETCH FIRST)
    import re

    base_query = re.sub(
        r'\s+FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\s*$',
        '',
        query_text.rstrip().rstrip(';'),
        flags=re.IGNORECASE,
    )

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            execute_suiteql_via_rest(
                access_token, account_id, base_query,
                limit=100_000, paginate=True,
            )
        )
        columns = result.get("columns", [])
        all_rows = result.get("rows", [])
    finally:
        loop.close()

    # Write CSV
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in query_name)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_name}_{timestamp}_{uuid.uuid4().hex[:8]}.csv"
    filepath = EXPORT_DIR / filename

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(all_rows)

    return {
        "file_path": str(filepath),
        "file_name": filename,
        "row_count": len(all_rows),
        "column_count": len(columns),
        "message": f"Exported {len(all_rows)} rows to {filename}",
    }
