"""Durable candidate discovery; collected pages never authorize cached money proof.

A page, its current owners, and each old-owner index page are checkpointed before
consumption. The outer runner saves pending references with this state and drains
those references before advancing again. Restarting may repeat a bounded read,
but cannot advance past unpersisted candidates.
"""

from app.services.transaction_ops.netsuite_changes import _REFERENCE
from app.services.transaction_ops.netsuite_dependency_changes import STREAMS
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError


async def advance(progress, *, read_page, read_owners, indexed_owners, unobserved, staging=None):
    async def put(value):
        return await staging.put(value) if staging else value

    async def get(value):
        return await staging.get(value) if staging else value

    scan = progress.setdefault("dependency_scan", {"version": 1, "stream_index": 0, "after": None})
    if (
        scan.get("version") != 1
        or type(scan.get("stream_index")) is not int
        or not 0 <= scan["stream_index"] <= len(STREAMS)
    ):
        raise NetSuiteEvidenceError("dependency_checkpoint_invalid")
    if scan["stream_index"] == len(STREAMS):
        progress["destination_scan_complete"] = True
        progress["dependency_scan_complete"] = True
        return
    stream = STREAMS[scan["stream_index"]]
    page = await get(scan.get("page"))
    if page is None:
        try:
            page = await read_page(stream, scan["after"])
        except NetSuiteEvidenceError as exc:
            size = progress.get("dependency_page_size", 250)
            if not staging or str(exc) != "dependency_change_batch_timeout" or size <= 20:
                raise
            progress["dependency_page_size"] = max(20, size // 2)
            progress["dependency_page_splits"] = progress.get("dependency_page_splits", 0) + 1
            return
        if page.get("stream") != stream or page.get("page_complete") is not True:
            raise NetSuiteEvidenceError("dependency_change_page_incomplete")
        scan["page"] = await put(page)
        if staging:
            progress["dependency_batches_staged"] = progress.get("dependency_batches_staged", 0) + 1
        progress["destination_scan_count"] = progress.get("destination_scan_count", 0) + len(page["changes"])
        return
    offset = scan.get("owner_offset", 0)
    chunk_size = scan.get("owner_chunk_size", 250)
    if (
        type(offset) is not int
        or not 0 <= offset <= len(page["changes"])
        or type(chunk_size) is not int
        or not 1 <= chunk_size <= 250
    ):
        raise NetSuiteEvidenceError("dependency_checkpoint_invalid")
    changes = page["changes"][offset : offset + chunk_size]
    keys = sorted({tuple(key) for change in changes for key in change["record_keys"]})
    if "owners" not in scan:
        # Deleted records cannot be resolved from the current native graph.
        # Their last observed owners come from the persisted additive index.
        if not keys or stream == "deletions":
            owners = []
        else:
            try:
                result = await read_owners(
                    document_ids=sorted({key[1] for key in keys if key[0] == "transaction"}),
                    order_ids=sorted({row["order_id"] for row in changes if row.get("order_id")}),
                    references=sorted(
                        {
                            row["order_reference"]
                            for row in changes
                            if isinstance(row.get("order_reference"), str)
                            and _REFERENCE.fullmatch(row["order_reference"])
                        }
                    ),
                )
            except (NetSuiteEvidenceError, TimeoutError) as exc:
                splittable = isinstance(exc, TimeoutError) or str(exc) in {
                    "dependency_owner_batch_timeout",
                    "dependency_owner_page_incomplete",
                    "dependency_owner_budget",
                    "dependency_owner_depth",
                    "read_timeout",
                    "dependency_stage_size",
                }
                if not staging or not splittable or len(changes) <= 1:
                    raise
                scan["owner_chunk_size"] = max(1, len(changes) // 2)
                progress["dependency_batch_splits"] = progress.get("dependency_batch_splits", 0) + 1
                return  # The outer lease checkpoint saves the smaller retry; no cursor advances.
            owners = result["order_references"]
        if (
            not isinstance(owners, list)
            or len(owners) > (2000 if staging else 400)
            or any(not isinstance(ref, str) or not _REFERENCE.fullmatch(ref) for ref in owners)
        ):
            raise NetSuiteEvidenceError("dependency_owner_page_incomplete")
        # Keep raw owner-query rows outside the 64 KiB progress checkpoint.
        inventory = result.get("inventory", []) if keys and stream != "deletions" else []
        stored = {"order_references": sorted(set(owners)), "inventory": inventory}
        try:
            stored_owners = await put(stored) if staging else stored["order_references"]
        except NetSuiteEvidenceError as exc:
            if str(exc) != "dependency_stage_size" or len(changes) <= 1:
                raise
            scan["owner_chunk_size"] = max(1, len(changes) // 2)
            progress["dependency_batch_splits"] = progress.get("dependency_batch_splits", 0) + 1
            return
        scan.update(owners=stored_owners, owner_position=0, index_after="", index_complete=not keys)
        return
    saved_owners = await get(scan["owners"])
    owners = saved_owners["order_references"] if isinstance(saved_owners, dict) else saved_owners
    position = scan.get("owner_position", 0)
    if type(position) is not int or not 0 <= position <= len(owners):
        raise NetSuiteEvidenceError("dependency_checkpoint_invalid")
    if position < len(owners) or not scan["index_complete"]:
        refs = owners[position : position + 100]
        scan["owner_position"] = min(position + 100, len(owners))
        if not scan["index_complete"]:
            indexed = await indexed_owners(keys, after_reference=scan["index_after"], page_size=100)
            refs += indexed["order_references"]
            scan["index_complete"] = not indexed["has_more"]
            scan["index_after"] = indexed["next_after_reference"] or scan["index_after"]
        # Exact streams end before the evidence reads. The deletion feed uses
        # an unzoned envelope, so require an observation after THIS page read.
        # An interim, stale, or cached finding cannot suppress a recheck.
        since = page["observed_at"] if stream == "deletions" else page["scope"]["window_end"]
        progress["pending_refs"] = await unobserved(sorted(set(refs)), since=since)
        progress["phase"] = "destination"
        return
    if offset + len(changes) < len(page["changes"]):
        scan["owner_offset"] = offset + len(changes)
        for key in ("owners", "owner_position", "index_after", "index_complete"):
            scan.pop(key, None)
        return
    if page["scan_complete"]:
        scan["stream_index"] += 1
        scan["after"] = None
        scan.pop("owner_chunk_size", None)
    else:
        scan["after"] = page["next_cursor"]
    for key in ("page", "owners", "owner_position", "owner_offset", "index_after", "index_complete"):
        scan.pop(key, None)
