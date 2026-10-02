"""Authenticated header batches prove routing only, never financial evidence.

The Framework list endpoint omits accounting detail. Missing/invalid rows are
not absence evidence and always retain the individual detail fallback.
"""

from urllib.parse import urlencode

from app.services.transaction_ops import source_snapshot
from app.services.transaction_ops.normalization import _time, source_entity_key
from app.services.transaction_ops.source_reader import _ORDER_REFERENCE, _direct_read

MAX_REFERENCES = 10


def _scopes(body, references, minimum_versions, observed_at):
    if not isinstance(body, dict) or not isinstance(body.get("orders"), list):
        return {}
    rows = body["orders"]
    metadata = {key: body.get(key) for key in ("current_page", "pages", "per_page", "total_count", "count")}
    if (
        any(type(value) is not int for value in metadata.values())
        or metadata["current_page"] != 1
        or metadata["pages"] not in ({0, 1} if not rows else {1})
        or metadata["per_page"] != MAX_REFERENCES
        or metadata["total_count"] != len(rows)
        or metadata["count"] != len(rows)
        or len(rows) > len(references)
    ):
        return {}
    seen, identities, scopes = set(), set(), {}
    for row in rows:
        if not isinstance(row, dict):
            return {}
        ref = row.get("number")
        if not isinstance(ref, str) or ref not in references or ref in seen:
            return {}  # An ignored filter or duplicate invalidates the entire batch.
        seen.add(ref)
        identifier = str(row.get("id", ""))
        if not identifier.isascii() or not identifier.isdigit() or len(identifier) > 30 or int(identifier) < 1:
            continue
        if int(identifier) in identities:
            return {}
        identities.add(int(identifier))
        try:
            updated = _time(row.get("updated_at"))
            completed = _time(row.get("completed_at"))
            entity = row.get("business_entity")
            identity = entity.get("id") if isinstance(entity, dict) else entity
            key = source_entity_key(row)
            if (
                "business_entity" not in row
                or (entity is not None and (type(identity) not in (str, int) or not str(identity).strip()))
                or key is None
                or updated is None
                or completed is None
                or updated > observed_at
                or completed > observed_at
            ):
                continue
            minimum = minimum_versions.get(ref)
            # Both APIs serialize their current version to milliseconds. This
            # exact fresh read may satisfy the replica's same-millisecond value.
            if minimum is not None and updated < minimum.replace(microsecond=minimum.microsecond // 1000 * 1000):
                continue
            scopes[ref] = key
        except (ValueError, TypeError, AttributeError, OverflowError):
            continue
    return scopes


async def read_order_scopes(db, tenant_id, connection_id, references, *, minimum_versions=None):
    if (
        not isinstance(references, (tuple, list))
        or not 1 <= len(references) <= MAX_REFERENCES
        or any(not isinstance(ref, str) or not _ORDER_REFERENCE.fullmatch(ref) for ref in references)
        or len(set(references)) != len(references)
    ):
        raise ValueError("invalid_source_scope_references")
    uri = "sync/orders?" + urlencode(
        [("q[number_in][]", ref) for ref in references] + [("page", "1"), ("per_page", str(MAX_REFERENCES))]
    )
    body, provenance = await _direct_read(db, tenant_id, connection_id, uri, client=None)
    # A revoke/rotation during HTTP invalidates routing just as it invalidates
    # full snapshots. This also checks the active tenant before any exclusion.
    if provenance.get("_connection_fingerprint") != await source_snapshot._connection(db, tenant_id, connection_id):
        return {}
    return _scopes(body, references, minimum_versions or {}, _time(provenance["read_at"]))
