"""Public HTTPS connectors: bounded reads, real verification, and secret isolation."""

import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.connection import ConnectionCreate
from app.services.http_connector_service import ConnectorReadError, read_json, validate_credentials
from app.services.public_http import PublicHTTPTransport, UnsafeEndpointError


@pytest.mark.parametrize(
    "url",
    [
        "http://shop.example/api",
        "https://user:secret@shop.example/",
        "https://shop.example/?key=secret",
        "https://shop.example/#secret",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/",
    ],
)
def test_rejects_unsafe_configuration(url):
    with pytest.raises((ValueError, ValidationError)):
        validate_credentials(
            "solidus", {"base_url": url, "auth_type": "bearer", "token": "secret", "api_profile": "solidus_rest"}
        )


@pytest.mark.parametrize("header", ["Host", "Cookie", "Content-Length", "X-Key\r\nHost", "Proxy-Authorization"])
def test_rejects_routing_and_injected_auth_headers(header):
    with pytest.raises(ValueError):
        validate_credentials(
            "api",
            {
                "base_url": "https://shop.example/api/",
                "auth_type": "api_key",
                "token": "secret",
                "header_name": header,
                "test_path": "health",
            },
        )


def test_solidus_provider_and_credentials_are_validated_without_echoing_secrets():
    request = ConnectionCreate(
        provider="solidus",
        label="Store",
        credentials={
            "base_url": "https://shop.example/api/",
            "auth_type": "bearer",
            "token": "secret",
            "api_profile": "solidus_rest",
        },
    )
    assert request.provider == "solidus"
    with pytest.raises(ValueError) as error:
        validate_credentials("solidus", {"token": "do-not-echo"})
    assert "do-not-echo" not in str(error.value)


@pytest.mark.asyncio
async def test_transport_pins_public_dns_and_preserves_tls_hostname(monkeypatch):
    lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr("app.services.public_http.resolve_addresses", lookup)
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = PublicHTTPTransport("https://shop.example/api/", transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://shop.example/api/orders")
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "shop.example"
    assert seen[0].extensions["sni_hostname"] == "shop.example"


@pytest.mark.asyncio
async def test_private_dns_or_changed_origin_never_reaches_network(monkeypatch):
    monkeypatch.setattr(
        "app.services.public_http.resolve_addresses",
        AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]),
    )
    upstream = AsyncMock()
    transport = PublicHTTPTransport("https://shop.example/api/", transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=transport) as client:
        for url in ("https://shop.example/api/orders", "https://evil.example/orders"):
            with pytest.raises(UnsafeEndpointError):
                await client.get(url)
    upstream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {}, "authentication_failed"),
        (302, {}, "http_error"),
        (429, {}, "rate_limited"),
        (200, {"errors": ["secret upstream detail"]}, "invalid_response"),
    ],
)
async def test_read_fails_closed_without_upstream_details(status, body, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))) as client:
        with pytest.raises(ConnectorReadError) as error:
            await read_json(
                {"base_url": "https://shop.example/api/", "auth_type": "bearer", "token": "secret"},
                "orders",
                client=client,
            )
    assert error.value.code == code
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_read_preserves_decimal_and_rejects_duplicate_keys():
    from decimal import Decimal

    credentials = {"base_url": "https://shop.example/api/", "auth_type": "bearer", "token": "secret"}

    def upstream(request):
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, content=b'{"total": 0.10}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        assert (await read_json(credentials, "orders", client=client))["total"] == Decimal("0.10")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'{"total":1,"total":2}'))
    ) as client:
        with pytest.raises(ConnectorReadError):
            await read_json(credentials, "orders", client=client)
