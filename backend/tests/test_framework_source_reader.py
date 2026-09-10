"""Bounded real source evidence: auth containment, completeness and decimal fidelity."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.services.transaction_ops.source_reader import (
    SourceReadError,
    read_framework_order,
    read_framework_orders_page,
)

TENANT = uuid.uuid4()
STEP = uuid.uuid4()
REMOTE_CONNECTION = "609c54e8a9a34b7255fa2ee9"
ORDER = "R123456789"
SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def source(monkeypatch):
    step = SimpleNamespace(id=STEP, connection_celigo_id=REMOTE_CONNECTION)
    connection = SimpleNamespace(
        id=uuid.uuid4(), encrypted_credentials="encrypted-secret", metadata_json={"region": "us"}
    )
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.one_or_none.return_value = (step, connection)
    monkeypatch.setattr(
        "app.services.transaction_ops.source_reader.decrypt_credentials", lambda _: {"token": "celigo-secret"}
    )
    return db


def live_connection(**changes):
    return {
        "_id": REMOTE_CONNECTION,
        "type": "http",
        "http": {"baseURI": "https://private-direct-access.frame.work/api/", "auth": {"type": "token"}},
        **changes,
    }


def preview(order=None):
    return {"data": [order or {"id": 10, "number": ORDER, "currency": "EUR", "total": "123.45"}]}


def page_preview(*, page=1, pages=2, total=3, page_size=2, orders=None):
    orders = (
        orders
        if orders is not None
        else [
            {"id": 1, "number": ORDER, "currency": "EUR"},
            {"id": 2, "number": "R123456780", "currency": "GBP"},
        ]
    )
    return {
        "data": [
            {
                "orders": orders,
                "current_page": page,
                "pages": pages,
                "per_page": page_size,
                "total_count": total,
                "count": len(orders),
            }
        ]
    }


def transport(responses, requests):
    def handle(request):
        requests.append(request)
        response = responses.pop(0)
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    return httpx.MockTransport(handle)


async def test_order_uses_fixed_read_preview_and_tenant_scoped_connection(source):
    requests = []
    async with httpx.AsyncClient(transport=transport([live_connection(), preview()], requests)) as client:
        result = await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    assert result["orders"][0]["number"] == ORDER
    assert result["source"] == "framework"
    assert result["page_complete"] is True
    assert len(requests) == 2
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"https://api.integrator.io/v1/connections/{REMOTE_CONNECTION}"
    assert requests[1].method == "POST"
    assert str(requests[1].url) == "https://api.integrator.io/v1/exports/preview"
    body = json.loads(requests[1].content)
    assert body == {
        "name": "Transaction evidence read",
        "_connectionId": REMOTE_CONNECTION,
        "type": "test",
        "test": {"limit": 1},
        "http": {"method": "GET", "relativeURI": f"sync/orders/{ORDER}", "followRedirects": False},
    }
    assert all(request.headers["authorization"] == "Bearer celigo-secret" for request in requests)
    sql = str(source.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "celigo_flow_steps.tenant_id =" in sql
    assert "celigo_flows.tenant_id =" in sql
    assert "connections.tenant_id =" in sql
    assert "celigo_flows.celigo_connection_id = celigo_flow_steps.celigo_connection_id" in sql
    assert "connections.provider =" in sql


async def test_page_proves_only_its_own_completeness(source):
    requests = []
    async with httpx.AsyncClient(transport=transport([live_connection(), page_preview()], requests)) as client:
        result = await read_framework_orders_page(source, TENANT, STEP, SINCE, page_size=2, client=client)
    assert result["page_complete"] is True
    assert result["window_complete"] is False
    assert result["next_page"] == 2
    assert result["total_count"] == 3
    assert result["orders"][0]["id"] == "1"
    body = json.loads(requests[1].content)
    assert body["http"]["relativeURI"] == (
        "sync/orders?q[updated_at_gteq]=2026-09-01T00%3A00%3A00Z&q[completed_at_not_null]=1&page=1&per_page=2&q[s]=id"
    )


async def test_keyset_read_binds_upper_watermark_and_validates_returned_ids(source):
    requests = []
    until = SINCE + timedelta(days=1)
    orders = [
        {"id": 11, "number": ORDER, "currency": "USD", "updated_at": SINCE.isoformat()},
        {"id": 12, "number": "R123456780", "currency": "USD", "updated_at": SINCE.isoformat()},
    ]
    async with httpx.AsyncClient(
        transport=transport(
            [
                live_connection(),
                page_preview(orders=orders, pages=1, total=2),
            ],
            requests,
        )
    ) as client:
        result = await read_framework_orders_page(
            source,
            TENANT,
            STEP,
            SINCE,
            page_size=2,
            client=client,
            after_id=10,
            updated_before=until,
        )
    uri = json.loads(requests[1].content)["http"]["relativeURI"]
    assert "q[id_gt]=10" in uri and "q[updated_at_lteq]=" in uri
    assert result["next_page"] is None
    assert result["window_complete"] is False  # a final suffix is not a whole-window proof


async def test_ignored_keyset_filter_cannot_be_accepted_as_new_data(source):
    requests = []
    async with httpx.AsyncClient(transport=transport([live_connection(), page_preview()], requests)) as client:
        with pytest.raises(SourceReadError, match="incomplete_page"):
            await read_framework_orders_page(source, TENANT, STEP, SINCE, page_size=2, client=client, after_id=10)


async def test_exact_decimal_projection_drops_raw_and_customer_objects(source):
    requests = []
    raw = (
        '{"data":[{"id":4,"number":"R123456789","total":123456789.123456789,'
        '"currency":"EUR","customer":{"password":"secret"},"headers":{"Authorization":"secret"},'
        '"line_items":[{"id":2,"quantity":3,"price":0.100000000000000001,"sku":"SKU-1",'
        '"raw":{"token":"secret"}}],"shipments":[{"number":"H123","cost":4.25,'
        '"address":{"email":"secret"}}]}],"request":{"token":"secret"}}'
    )
    async with httpx.AsyncClient(
        transport=transport([live_connection(), httpx.Response(200, content=raw)], requests)
    ) as client:
        result = await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    order = result["orders"][0]
    assert order["total"] == "123456789.123456789"
    assert order["line_items"][0]["price"] == "0.100000000000000001"
    assert "secret" not in json.dumps(result)
    assert order["shipments"][0]["cost"] == "4.25"


@pytest.mark.parametrize(
    "reference", ["R123", "../../admin", "R123456789?token=x", "{{number}}", "R123456789/x", "r123456789"]
)
async def test_invalid_order_reference_never_reaches_database(source, reference):
    with pytest.raises(SourceReadError, match="invalid_order_reference"):
        await read_framework_order(source, TENANT, STEP, reference)
    source.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": 0},
        {"page": 100001},
        {"page_size": 21},
        {"page_size": 0},
        {"page": True},
        {"updated_since": datetime(2026, 9, 1)},
    ],
)
async def test_page_input_bounds(source, kwargs):
    args = {"updated_since": SINCE, **kwargs}
    with pytest.raises(SourceReadError, match="invalid_page_request"):
        await read_framework_orders_page(source, TENANT, STEP, **args)
    source.execute.assert_not_awaited()


async def test_cross_tenant_or_missing_step_is_not_found_before_credentials(source):
    source.execute.return_value.one_or_none.return_value = None
    with pytest.raises(SourceReadError, match="source_not_found") as err:
        await read_framework_order(source, TENANT, STEP, ORDER)
    assert err.value.http_status == 404


@pytest.mark.parametrize(
    "connection",
    [
        live_connection(type="netsuite"),
        live_connection(http={"baseURI": "https://evil.test/api/"}),
        live_connection(http={"baseURI": "https://private-direct-access.frame.work/api/../admin"}),
        live_connection(_id="000000000000000000000000"),
    ],
)
async def test_wrong_live_source_never_previews(source, connection):
    requests = []
    async with httpx.AsyncClient(transport=transport([connection], requests)) as client:
        with pytest.raises(SourceReadError, match="unsupported_source"):
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {**preview(), "errors": [{"message": "secret"}]},
        {**preview(), "stages": [{"name": "source", "errors": [{"message": "secret"}]}]},
        {**preview(), "stages": [{"name": "source", "errors": "secret"}]},
        {**preview(), "truncated": True},
        {**preview(), "sampled": True},
        {**preview(), "stages": [{"name": "source", "truncated": True}]},
        {"data": []},
        {"data": [{"number": "R999999999"}]},
        {"data": [{"order": {"number": ORDER}}]},
    ],
)
async def test_errors_sampling_and_wrong_identity_fail_without_raw_leak(source, body):
    requests = []
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        with pytest.raises(SourceReadError) as err:
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    assert "secret" not in str(err.value)


@pytest.mark.parametrize(
    "change",
    [
        {"count": 3},
        {"count": True},
        {"pages": 9},
        {"per_page": 20},
        {"current_page": 2},
        {"total_count": None},
        {"orders": [{"number": ORDER}]},
        {"orders": [{"number": ORDER}, {"number": ORDER}]},
    ],
)
async def test_partial_or_malformed_page_never_proves_complete(source, change):
    body = page_preview()
    body["data"][0].update(change)
    requests = []
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        with pytest.raises(SourceReadError, match="incomplete_page"):
            await read_framework_orders_page(source, TENANT, STEP, SINCE, page_size=2, client=client)


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
async def test_http_failures_never_leak_body_or_follow_redirect(source, status):
    requests = []
    response = httpx.Response(status, text="secret-token", headers={"Location": "https://evil.test"})
    async with httpx.AsyncClient(transport=transport([response], requests), follow_redirects=True) as client:
        with pytest.raises(SourceReadError) as err:
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    assert len(requests) == 1
    assert "secret-token" not in str(err.value)


async def test_response_size_is_bounded(source):
    requests = []
    response = httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))
    async with httpx.AsyncClient(transport=transport([live_connection(), response], requests)) as client:
        with pytest.raises(SourceReadError, match="response_too_large"):
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)


async def test_live_business_shape_preserves_payment_exchange_evidence_without_credentials(source):
    requests = []
    body = preview(
        {
            "number": ORDER,
            "currency": "EUR",
            "total": "2009.00",
            "total_quantity": 9,
            "item_total": "2009.00",
            "included_tax_total": "348.68",
            "additional_tax_total": "0.00",
            "credit_cards": [{"token": "secret"}],
            "token": "secret",
            "admin_metadata": {"password": "secret"},
            "line_items": [
                {
                    "id": 2,
                    "parent_id": 1,
                    "quantity": 1,
                    "price": "2009.00",
                    "variant": {"sku": "FRAME-1", "name": "Product", "description": "secret"},
                    "adjustments": [
                        {"amount": "348.68", "source_type": "Spree::TaxRate", "finalized": True, "source_id": 44}
                    ],
                }
            ],
            "payments": [
                {
                    "id": 1,
                    "source_type": "Spree::CreditCard",
                    "source_id": 2,
                    "amount": "2009.00",
                    "exchange_rate": "0.85202000123",
                    "state": "completed",
                    "source": {"token": "secret"},
                    "avs_response": "secret",
                    "payment_method": {"credentials": "secret"},
                }
            ],
        }
    )
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        result = await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    order = result["orders"][0]
    assert order["total_quantity"] == "9"
    assert order["line_items"][0]["parent_id"] == "1"
    assert order["line_items"][0]["adjustments"][0]["finalized"] is True
    assert order["payments"][0]["exchange_rate"] == "0.85202000123"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("orders,total,pages", [([], 0, 0), ([], 0, 1), ([{"number": ORDER}], 1, 1)])
async def test_complete_single_page_window(source, orders, total, pages):
    requests = []
    body = page_preview(orders=orders, total=total, pages=pages, page_size=20)
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        result = await read_framework_orders_page(source, TENANT, STEP, SINCE, client=client)
    assert result["window_complete"] is True
    assert result["next_page"] is None


async def test_last_page_does_not_claim_prior_pages_were_read(source):
    requests = []
    body = page_preview(orders=[{"number": ORDER}], page=2)
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        result = await read_framework_orders_page(source, TENANT, STEP, SINCE, page=2, page_size=2, client=client)
    assert result["window_complete"] is False
    assert result["next_page"] is None


async def test_transport_failure_discards_sensitive_exception_text(source):
    def fail(request):
        raise httpx.ReadTimeout("secret-token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SourceReadError, match="source_transport_failed") as err:
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    assert "secret" not in str(err.value)


async def test_operation_deadline_bounds_slow_requests(source, monkeypatch):
    import asyncio

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=live_connection())

    monkeypatch.setattr("app.services.transaction_ops.source_reader._OPERATION_TIMEOUT", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(SourceReadError, match="source_transport_failed"):
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)


@pytest.mark.parametrize("raw", ['{"data":[],"data":[]}', '{"data":[NaN]}', '{"data":[Infinity]}', "not-json"])
async def test_invalid_json_never_surfaces_raw(source, raw):
    requests = []
    async with httpx.AsyncClient(
        transport=transport([live_connection(), httpx.Response(200, content=raw)], requests)
    ) as client:
        with pytest.raises(SourceReadError, match="invalid_source_response"):
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)


async def test_unknown_region_and_invalid_saved_id_fail_before_transport(source):
    step, connection = source.execute.return_value.one_or_none.return_value
    connection.metadata_json = {"region": "https://evil.test"}
    with pytest.raises(SourceReadError, match="unsupported_source_region"):
        await read_framework_order(source, TENANT, STEP, ORDER)
    connection.metadata_json = {"region": "us"}
    step.connection_celigo_id = "../../tokenInfo"
    with pytest.raises(SourceReadError, match="unsupported_source"):
        await read_framework_order(source, TENANT, STEP, ORDER)


@pytest.mark.parametrize(
    "field,value", [("total", {"token": "secret"}), ("line_items", [{"id": 1}] * 501), ("total", "1" * 2049)]
)
async def test_oversized_or_malformed_known_fields_are_not_silently_truncated(source, field, value):
    requests = []
    body = preview({"number": ORDER, field: value})
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        with pytest.raises(SourceReadError, match="invalid_business_evidence"):
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)


async def test_documented_null_stage_errors_are_clean(source):
    requests = []
    body = {**preview(), "stages": [{"name": "source", "errors": None}]}
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        result = await read_framework_order(source, TENANT, STEP, ORDER, client=client)
    assert result["page_complete"] is True


@pytest.mark.parametrize("marker", [{"errors": [{"message": "secret"}]}, {"truncated": True}])
async def test_per_order_page_errors_cannot_be_projected_into_complete_evidence(source, marker):
    requests = []
    body = page_preview(orders=[{"number": ORDER, **marker}], total=1, pages=1, page_size=20)
    async with httpx.AsyncClient(transport=transport([live_connection(), body], requests)) as client:
        with pytest.raises(SourceReadError) as err:
            await read_framework_orders_page(source, TENANT, STEP, SINCE, client=client)
    assert "secret" not in str(err.value)


async def test_extreme_numeric_exponent_remains_controlled_source_error(source):
    requests = []
    raw = '{"data":[{"number":"R123456789","total":1e999999999999999999999}]}'
    async with httpx.AsyncClient(
        transport=transport([live_connection(), httpx.Response(200, content=raw)], requests)
    ) as client:
        with pytest.raises(SourceReadError, match="invalid_source_response"):
            await read_framework_order(source, TENANT, STEP, ORDER, client=client)
