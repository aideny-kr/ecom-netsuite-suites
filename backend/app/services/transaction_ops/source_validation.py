"""Validate persisted full detail against its exact authenticated HTTP resource.

A 304 is a new provider validation of the saved representation, not a TTL guess.
Original body collection time survives; write preflights do not use this reader.
"""

import copy
import hashlib
from datetime import datetime, timezone

from app.services.http_connector_service import ConnectorReadError, read_json_response
from app.services.transaction_ops import source_snapshot
from app.services.transaction_ops.source_reader import (
    _ORDER_REFERENCE,
    SourceReadError,
    _check_envelope,
    _project_orders,
    direct_connection,
)


async def read_validated_order(db, tenant_id, step_id, reference, *, source_connection_id, client=None):
    if (
        step_id is not None
        or not isinstance(reference, str)
        or len(reference) > 100
        or not _ORDER_REFERENCE.fullmatch(reference)
    ):
        raise SourceReadError("invalid_order_reference", 422)
    now = datetime.now(timezone.utc)
    cached = await source_snapshot.load_for_validation(db, tenant_id, source_connection_id, reference, now=now)
    connection, credentials = await direct_connection(db, tenant_id, source_connection_id)
    fingerprint = hashlib.sha256(connection.encrypted_credentials.encode()).hexdigest()
    # The second authorization read must still match the snapshot's credential
    # partition. A rotation between cache lookup and HTTP cannot reuse old scope.
    if cached and cached.pop("_validation_connection_fingerprint", None) != fingerprint:
        cached = None
    try:
        order, etag, unchanged = await read_json_response(
            credentials,
            f"sync/orders/{reference}",
            client=client,
            if_none_match=cached["_source_etag"] if cached else None,
        )
    except ConnectorReadError as error:
        raise SourceReadError("source_" + error.code, 429 if error.code == "rate_limited" else 502) from None
    observed = datetime.now(timezone.utc).isoformat()
    if unchanged:
        if cached is None:
            raise SourceReadError("source_invalid_validator_response")
        result = copy.deepcopy(cached)
        result.update(read_at=observed, source_validation="etag_not_modified")
    else:
        _check_envelope(order)
        if not isinstance(order, dict) or order.get("number") != reference:
            raise SourceReadError("order_identity_mismatch")
        result = {
            "source": "framework",
            "source_transport": "solidus_direct",
            "connection_id": str(connection.id),
            "read_at": observed,
            "body_collected_at": observed,
            "scope": "order",
            "orders": _project_orders([order]),
            "page_complete": True,
            "window_complete": False,
            "next_page": None,
        }
    result["_connection_fingerprint"] = fingerprint
    if etag:
        result["_source_etag"] = etag
    return result
