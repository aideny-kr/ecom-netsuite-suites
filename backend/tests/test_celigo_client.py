"""Celigo REST client — token verification only (Plan A scope)."""

import httpx
import pytest

from app.services.celigo.client import CELIGO_BASE_URLS, CeligoAuthError, CeligoError, verify_token


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestRegionRouting:
    def test_both_regions_registered(self):
        assert CELIGO_BASE_URLS["us"] == "https://api.integrator.io"
        assert CELIGO_BASE_URLS["eu"] == "https://api.eu.integrator.io"

    @pytest.mark.asyncio
    async def test_eu_region_hits_eu_host(self):
        """EU tenants are fully isolated; a US call against an EU account 401s."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"name": "Acme", "email": "a@b.co"})

        async with _client(handler) as c:
            await verify_token("tok", region="eu", client=c)

        assert seen["url"].startswith("https://api.eu.integrator.io/v1/tokenInfo")

    @pytest.mark.asyncio
    async def test_us_is_the_default_region(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"name": "Acme", "email": "a@b.co"})

        async with _client(handler) as c:
            await verify_token("tok", client=c)

        assert seen["url"].startswith("https://api.integrator.io/v1/tokenInfo")


class TestAuth:
    @pytest.mark.asyncio
    async def test_bearer_header_sent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"name": "Acme", "email": "a@b.co"})

        async with _client(handler) as c:
            await verify_token("s3cret", client=c)

        assert seen["auth"] == "Bearer s3cret"

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        """Celigo returns {message} on 401, NOT the standard {errors:[...]} envelope.

        Parsing it as {errors:[...]} yields a KeyError and a 500 instead of a
        clean 'your token is wrong' message.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid token"})

        async with _client(handler) as c:
            with pytest.raises(CeligoAuthError) as exc:
                await verify_token("bad", client=c)

        assert "Invalid token" in str(exc.value)

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Forbidden"})

        async with _client(handler) as c:
            with pytest.raises(CeligoAuthError):
                await verify_token("scoped-too-tight", client=c)


class TestExceptionHierarchy:
    def test_auth_error_is_not_a_subclass_of_celigo_error(self):
        """connector_status.connect_celigo catches CeligoAuthError BEFORE
        (CeligoError, httpx.HTTPError) so a bad token returns 400 (operator
        error) rather than 502 (upstream outage). That ordering is safe ONLY
        because the two are siblings, both inheriting directly from Exception.

        Making CeligoAuthError(CeligoError) is an entirely natural-looking
        future refactor ("auth errors ARE Celigo errors") -- and it would
        silently reclassify every bad-token 400 as a 502, with the test suite
        staying green because each except branch is exercised through a
        different mock. This assertion is the tripwire for that refactor.
        """
        assert issubclass(CeligoAuthError, CeligoError) is False


class TestSuccess:
    @pytest.mark.asyncio
    async def test_returns_account_identity(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"name": "Framework", "email": "ops@frame.work"})

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info == {"account_name": "Framework", "user_email": "ops@frame.work"}

    @pytest.mark.asyncio
    async def test_missing_fields_degrade_gracefully(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info == {"account_name": "", "user_email": ""}
