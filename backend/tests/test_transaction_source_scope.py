"""Routing headers cannot become detail evidence or exclude unknown candidates."""

import hashlib
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import http_connector_service
from app.services.transaction_ops import source_scope as scope
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_transaction_source_snapshot import NOW, REF, seed

OTHER = "R987654321"


def envelope():
    return {
        "current_page": 1,
        "pages": 1,
        "per_page": 10,
        "total_count": 1,
        "count": 1,
        "orders": [
            {
                "id": 1,
                "number": REF,
                "business_entity": {"id": "other"},
                "updated_at": (NOW - timedelta(hours=1)).isoformat(),
                "completed_at": (NOW - timedelta(days=1)).isoformat(),
                "payments": ["must-not-be-used"],
            }
        ],
    }


def test_missing_orders_are_not_absence_and_header_never_becomes_detail():
    assert scope._scopes(envelope(), [REF, OTHER], {}, NOW) == {REF: "other"}


@pytest.mark.parametrize("fault", ["extra", "duplicate", "metadata", "bool", "truncated", "nonobject"])
def test_ignored_filters_and_invalid_batches_retain_all_candidates(fault):
    body = envelope()
    if fault == "extra":
        body["orders"][0]["number"] = OTHER
    elif fault == "duplicate":
        body["orders"] *= 2
        body.update(count=2, total_count=2)
    elif fault == "metadata":
        body["total_count"] = 20
    elif fault == "bool":
        body["pages"] = True
    elif fault == "truncated":
        body["pages"] = 2
    else:
        body["orders"] = [None]
    assert scope._scopes(body, [REF], {}, NOW) == {}


@pytest.mark.parametrize(
    "fault",
    [
        "missing_entity",
        "unknown_entity",
        "bool_entity",
        "empty_entity",
        "missing_id",
        "stale",
        "future",
        "incomplete",
        "naive",
    ],
)
def test_ambiguous_or_old_scope_requires_detail(fault):
    body = envelope()
    row = body["orders"][0]
    minimum = {}
    if fault == "missing_entity":
        row.pop("business_entity")
    elif fault == "unknown_entity":
        row["business_entity"] = {}
    elif fault == "bool_entity":
        row["business_entity"] = True
    elif fault == "empty_entity":
        row["business_entity"] = ""
    elif fault == "missing_id":
        row.pop("id")
    elif fault == "stale":
        minimum[REF] = NOW
    elif fault == "future":
        row["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif fault == "incomplete":
        row["completed_at"] = None
    else:
        row["updated_at"] = NOW.replace(tzinfo=None).isoformat()
    assert scope._scopes(body, [REF], minimum, NOW) == {}


def test_explicit_legacy_and_fresh_same_millisecond_version():
    body = envelope()
    row = body["orders"][0]
    row.update(business_entity=None, updated_at=NOW.isoformat())
    assert scope._scopes(body, [REF], {REF: NOW + timedelta(microseconds=1)}, NOW) == {REF: "legacy"}


@pytest.mark.parametrize("fault", [None, "tenant", "connection", "revoked", "rotation"])
async def test_live_header_authorization_partition_is_preserved(db, admin_user, monkeypatch, fault):
    actor, _ = admin_user
    conn, _ = await seed(db, actor.tenant_id)
    original = conn.encrypted_credentials

    async def read(*args, **kwargs):
        if fault == "revoked":
            conn.status = "revoked"
            await db.flush()
        if fault == "rotation":
            other, _ = await seed(db, actor.tenant_id)
            conn.encrypted_credentials = other.encrypted_credentials
            assert conn.encrypted_credentials != original
            await db.flush()
        return deepcopy(envelope())

    http = AsyncMock(side_effect=read)
    monkeypatch.setattr(http_connector_service, "read_json", http)
    tenant = uuid4() if fault == "tenant" else actor.tenant_id
    connection = uuid4() if fault == "connection" else conn.id
    if fault in {"tenant", "connection", "revoked"}:
        with pytest.raises(SourceReadError, match="source_not_found"):
            await scope.read_order_scopes(db, tenant, connection, [REF])
        if fault != "revoked":
            http.assert_not_awaited()
    else:
        result = await scope.read_order_scopes(db, tenant, connection, [REF])
        assert result == ({} if fault == "rotation" else {REF: "other"})
        assert "q%5Bnumber_in%5D%5B%5D=" in http.call_args.args[1]
        assert "per_page=10" in http.call_args.args[1]
        assert hashlib.sha256(original.encode()).hexdigest() not in str(result)


@pytest.mark.parametrize("refs", [[], [REF] * 2, [REF] * 11, ["invalid"], [None]])
async def test_invalid_request_never_sends(refs, monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(scope, "_direct_read", reader)
    with pytest.raises(ValueError, match="invalid_source_scope_references"):
        await scope.read_order_scopes(None, uuid4(), uuid4(), refs)
    reader.assert_not_awaited()


def test_two_requested_numbers_cannot_share_one_source_identity():
    body = envelope()
    second = deepcopy(body["orders"][0])
    second["number"] = OTHER
    body["orders"].append(second)
    body.update(count=2, total_count=2)
    assert scope._scopes(body, [REF, OTHER], {}, NOW) == {}
