"""Run after migration 115: python -m scripts.backfill_review_metadata --tenant-id UUID --max-rows N.

Only derived metadata is written. Batches commit independently, skip locked
findings and are safe to resume. Exit 2 means rows remain (budget or row locks).
"""

import argparse
import asyncio
import json
from uuid import UUID

from app.core.database import async_session_factory, engine
from app.services.transaction_ops.review_metadata import backfill_batch


async def run(tenant_id, max_rows):
    total = 0
    engine.echo = False
    try:
        async with async_session_factory() as db:
            while total < max_rows:
                result = await backfill_batch(db, tenant_id, limit=min(500, max_rows - total))
                total += result["updated"]
                print(json.dumps({**result, "total_updated": total}), flush=True)
                if result["remaining"] == 0:
                    return 0
                if result["updated"] == 0:
                    return 2
        return 2
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--max-rows", type=int, required=True)
    options = parser.parse_args()
    if not 1 <= options.max_rows <= 1_000_000:
        parser.error("--max-rows must be between 1 and 1000000")
    raise SystemExit(asyncio.run(run(options.tenant_id, options.max_rows)))
