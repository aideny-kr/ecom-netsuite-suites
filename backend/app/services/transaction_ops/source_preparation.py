"""Bounded source preparation; only the caller owns budgets and the run cursor.

Every branch has its own tenant-scoped session and transport. Completed reads
are durably staged even if a sibling fails. Exceptions stay local until the
coordinator can apply the existing single-read recovery policy.
"""

import asyncio

from app.core.database import set_tenant_context_session, worker_async_session
from app.services.transaction_ops import source_snapshot, source_validation
from app.services.transaction_ops.read_transport import CollectionTransport, collection_transport
from app.services.transaction_ops.source_reader import direct_connection

MAX_WORKERS = 4


async def concurrency(db, tenant_id, connection_id):
    connection, _ = await direct_connection(db, tenant_id, connection_id)
    metadata = connection.metadata_json
    value = metadata.get("recon_prepare_concurrency", 1) if isinstance(metadata, dict) else 1
    return value if type(value) is int and 1 <= value <= MAX_WORKERS else 1


async def joined(*operations):
    """Never let a failed/cancelled branch outlive the coordinator's reservation."""
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            # Cancelling gather already cancels its children. A second cancel
            # can interrupt AsyncSession's rollback/close and leak connections.
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def collect(tenant_id, connection_id, references, *, workers, check_active, clock):
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("invalid_source_preparation_concurrency")
    if not 1 <= len(references) <= 10 or len(set(references)) != len(references):
        raise ValueError("invalid_source_preparation_references")
    queue = iter(references)
    results = {}
    stopped = False
    active = peak = 0

    async def worker():
        nonlocal stopped, active, peak
        transport = CollectionTransport()
        try:
            async with worker_async_session(pin_connection=True) as db:
                await set_tenant_context_session(db, str(tenant_id))
                with collection_transport(transport):
                    while not stopped:
                        ref = next(queue, None)
                        if ref is None:
                            return
                        active += 1
                        peak = max(peak, active)
                        try:
                            await check_active(db)
                            observed = await source_validation.read_validated_order(
                                db, tenant_id, None, ref, source_connection_id=connection_id
                            )
                            await source_snapshot.save(db, tenant_id, connection_id, ref, observed, now=clock())
                            results[ref] = observed
                        except Exception as error:
                            results[ref] = error
                            stopped = True  # Do not start queued reads after a failure/revocation.
                        finally:
                            active -= 1
        finally:
            await transport.aclose()

    await joined(*(worker() for _ in range(min(workers, len(references)))))
    return results, peak
