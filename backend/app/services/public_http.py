"""Public HTTPS transport with DNS pinning and an immutable credential origin."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class UnsafeEndpointError(ValueError):
    def __init__(self):
        super().__init__("Use a public HTTPS endpoint without URL credentials, queries or fragments")


def validate_endpoint(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            not isinstance(value, str)
            or len(value) > 1024
            or any(ord(char) < 33 for char in value)
            or "\\" in value
            or url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.port not in (None, 443)
            or url.hostname.rstrip(".").lower() in ("localhost", "metadata.google.internal")
        ):
            raise UnsafeEndpointError()
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise UnsafeEndpointError()
        return str(httpx.URL(value))
    except (TypeError, ValueError):
        raise UnsafeEndpointError() from None


async def resolve_addresses(host, port):
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    """Resolve every request, reject mixed private/public DNS, dial the checked IP.

    Host and TLS SNI retain the original name. A single transport is scoped to one
    origin, so redirects and MCP-advertised endpoints cannot move credentials.
    """

    def __init__(self, endpoint: str, *, transport=None):
        self.origin = httpx.URL(validate_endpoint(endpoint))
        self.transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (request.url.scheme, request.url.host, request.url.port) != (
            self.origin.scheme,
            self.origin.host,
            self.origin.port,
        ):
            raise UnsafeEndpointError()
        try:
            addresses = await asyncio.wait_for(resolve_addresses(request.url.host, 443), timeout=5)
            ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
            if not ips or any(not address.is_global for address in ips):
                raise UnsafeEndpointError()
        except (OSError, ValueError, TimeoutError):
            raise UnsafeEndpointError() from None
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=str(ips[0])),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return await self.transport.handle_async_request(pinned)

    async def aclose(self):
        await self.transport.aclose()
