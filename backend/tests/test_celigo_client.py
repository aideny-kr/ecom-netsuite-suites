"""Celigo REST client — token verification only (Plan A scope)."""

import httpx
import pytest

from app.services.celigo.client import (
    CELIGO_BASE_URLS,
    CeligoAuthError,
    CeligoError,
    mcp_server_url,
    verify_token,
)


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


class TestMcpServerUrlRegionRouting:
    """FIX 2 (T2 gate round 3, PR #202): the celigo_mcp connector's server_url
    used to be a single hardcoded US host regardless of the connection's
    region, so an EU tenant's agent token got authenticated against the wrong
    MCP host and discovery silently failed. mcp_server_url() mirrors
    base_url()'s region → host derivation exactly, off the same
    CELIGO_BASE_URLS constant, so the two can never drift apart again.
    """

    def test_us_region_returns_us_mcp_host(self):
        assert mcp_server_url("us") == "https://api.integrator.io/celigo-mcp"

    def test_eu_region_returns_eu_mcp_host(self):
        assert mcp_server_url("eu") == "https://api.eu.integrator.io/celigo-mcp"

    def test_unknown_region_falls_back_to_us_mcp_host(self):
        # Mirrors base_url()'s own fallback -- an unrecognized region must
        # never silently resolve to no URL at all.
        assert mcp_server_url("mars") == "https://api.integrator.io/celigo-mcp"


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
    async def test_one_identity_field_present_is_enough(self):
        """At least ONE of name/email is required, not both."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"name": "Framework"})

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info == {"account_name": "Framework", "user_email": ""}


class TestNonGenuineSuccess:
    """MINOR (T2 gate on PR #202): verify_token previously treated any status
    < 400 as success -- including 3xx and 204 -- and a 200 with an empty or
    unparseable body degraded to {"account_name": "", "user_email": ""}
    rather than raising. That means a misconfigured/compromised Celigo
    endpoint (or a proxy returning a redirect/204) could make an invalid or
    meaningless token look "verified". A genuine success now requires a 2xx
    status AND a parseable JSON object body carrying at least one identity
    field; anything else raises CeligoError (never CeligoAuthError -- this is
    not evidence the token itself is bad, just that Celigo's response
    couldn't prove an identity)."""

    @pytest.mark.asyncio
    async def test_204_is_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with _client(handler) as c:
            with pytest.raises(CeligoError):
                await verify_token("tok", client=c)

    @pytest.mark.asyncio
    async def test_3xx_is_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://example.com"})

        async with _client(handler) as c:
            with pytest.raises(CeligoError):
                await verify_token("tok", client=c)

    @pytest.mark.asyncio
    async def test_200_with_non_json_body_is_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        async with _client(handler) as c:
            with pytest.raises(CeligoError):
                await verify_token("tok", client=c)

    @pytest.mark.asyncio
    async def test_200_with_a_json_list_body_is_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "a", "dict"])

        async with _client(handler) as c:
            with pytest.raises(CeligoError):
                await verify_token("tok", client=c)

    @pytest.mark.asyncio
    async def test_200_with_completely_empty_identity_is_not_success(self):
        """A 200 with neither name nor email carries no proof of anything --
        this used to silently degrade to {"account_name": "", "user_email": ""}."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        async with _client(handler) as c:
            with pytest.raises(CeligoError):
                await verify_token("tok", client=c)
