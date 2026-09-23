"""Durable candidate discovery; collected pages never authorize cached money proof.

A page, its current owners, and each old-owner index page are checkpointed before
consumption. The outer runner saves pending references with this state and drains
those references before advancing again. Restarting may repeat a bounded read,
but cannot advance past unpersisted candidates.
"""

from app.services.transaction_ops.netsuite_changes import _REFERENCE
from app.services.transaction_ops.netsuite_dependency_changes import STREAMS
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError


async def advance(progress, *, read_page, read_owners, indexed_owners, unobserved):
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
    page = scan.get("page")
    if page is None:
        page = await read_page(stream, scan["after"])
        if page.get("stream") != stream or page.get("page_complete") is not True:
            raise NetSuiteEvidenceError("dependency_change_page_incomplete")
        scan["page"] = page
        progress["destination_scan_count"] = progress.get("destination_scan_count", 0) + len(page["changes"])
        return
    keys = sorted({tuple(key) for change in page["changes"] for key in change["record_keys"]})
    if "owners" not in scan:
        # Deleted records cannot be resolved from the current native graph.
        # Their last observed owners come from the persisted additive index.
        if not keys or stream == "deletions":
            owners = []
        else:
            result = await read_owners(
                document_ids=sorted({key[1] for key in keys if key[0] == "transaction"}),
                order_ids=sorted({row["order_id"] for row in page["changes"] if row.get("order_id")}),
                references=sorted(
                    {
                        row["order_reference"]
                        for row in page["changes"]
                        if isinstance(row.get("order_reference"), str) and _REFERENCE.fullmatch(row["order_reference"])
                    }
                ),
            )
            owners = result["order_references"]
        if (
            not isinstance(owners, list)
            or len(owners) > 400
            or any(not isinstance(ref, str) or not _REFERENCE.fullmatch(ref) for ref in owners)
        ):
            raise NetSuiteEvidenceError("dependency_owner_page_incomplete")
        scan.update(owners=sorted(set(owners)), index_after="", index_complete=not keys)
        return
    if scan["owners"] or not scan["index_complete"]:
        refs = scan["owners"][:100]
        scan["owners"] = scan["owners"][100:]
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
    if page["scan_complete"]:
        scan["stream_index"] += 1
        scan["after"] = None
    else:
        scan["after"] = page["next_cursor"]
    for key in ("page", "owners", "index_after", "index_complete"):
        scan.pop(key, None)
