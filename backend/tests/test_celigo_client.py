"""Celigo REST client — token verification (Plan A) plus paginated, projected
resource fetchers (Plan B, Task 3)."""

import inspect

import httpx
import pytest

from app.services.celigo.client import (
    _MAX_ERROR_PAGES,
    _MAX_LIST_PAGES,
    CELIGO_BASE_URLS,
    CeligoAuthError,
    CeligoError,
    CeligoIncompleteListingError,
    get_resource,
    list_error_summary_for_integration,
    list_flow_error_summary,
    list_flow_errors_for_step,
    list_resource,
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
            return httpx.Response(200, json={"_userId": "5fe26cb4b32d987eb607618e"})

        async with _client(handler) as c:
            await verify_token("tok", region="eu", client=c)

        assert seen["url"].startswith("https://api.eu.integrator.io/v1/tokenInfo")

    @pytest.mark.asyncio
    async def test_us_is_the_default_region(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"_userId": "5fe26cb4b32d987eb607618e"})

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
            return httpx.Response(200, json={"_userId": "5fe26cb4b32d987eb607618e"})

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
        """Display fields are passed through WHEN present -- but they are not
        what proves identity, and the live endpoint omits them entirely (see
        TestDocumentedTokenInfoShape)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"_userId": "u-framework", "name": "Framework", "email": "ops@frame.work"},
            )

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info == {
            "user_id": "u-framework",
            "scope": "",
            "account_name": "Framework",
            "user_email": "ops@frame.work",
        }

    @pytest.mark.asyncio
    async def test_absent_display_fields_are_empty_not_an_error(self):
        """The real-world case: ``_userId`` alone is a complete, verified
        identity. Display fields default to empty rather than failing."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"_userId": "u-framework"})

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info == {
            "user_id": "u-framework",
            "scope": "",
            "account_name": "",
            "user_email": "",
        }


class TestDocumentedTokenInfoShape:
    """REGRESSION (found live 2026-08-27, against the real Framework account).

    Celigo's ``GET /v1/tokenInfo`` returns ``{"_userId": ...}`` and optionally
    ``"scope"``. It does NOT return ``name`` or ``email`` -- see
    https://github.com/celigo/integrator-api-docs. Every fixture in this file
    invented those two fields, and a comment in
    ``tests/api/test_celigo_connector_status.py`` codified the invention as a
    guarantee ("tokenInfo guarantees at least one of name/email"). So 5936
    tests passed against a response shape Celigo never sends, while
    ``connect_celigo`` raised ``CeligoError`` for every real token and the
    feature could not be connected at all.

    ``_userId`` is also a STRICTLY better identity key than ``account_name``:
    it is a stable id rather than a display string Celigo never promised to be
    unique, which is what let the old ``_celigo_accounts_match`` docstring
    concede it was "NOT a cryptographic proof of account identity".
    """

    @pytest.mark.asyncio
    async def test_documented_response_is_a_verified_identity(self):
        """The exact body the live endpoint returned for a real service token."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"_userId": "5fe26cb4b32d987eb607618e"})

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info["user_id"] == "5fe26cb4b32d987eb607618e"

    @pytest.mark.asyncio
    async def test_scope_is_surfaced_when_celigo_reports_it(self):
        """``scope`` is Celigo stating the token's permissions itself -- a
        stronger read-only signal than trusting tool names reported by a
        remote MCP server."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"_userId": "u1", "scope": "*"})

        async with _client(handler) as c:
            info = await verify_token("tok", client=c)

        assert info["scope"] == "*"

    @pytest.mark.asyncio
    async def test_missing_user_id_is_not_a_verified_identity(self):
        """Fail closed: no ``_userId`` means the documented identity field is
        absent, so nothing was proven -- even if display fields are present."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"name": "Framework"})

        async with _client(handler) as c:
            with pytest.raises(CeligoError):
                await verify_token("tok", client=c)


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


# ============================================================================
# Task 3: paginated, projected resource fetchers.
#
# SECURITY MODEL CORRECTION (see app/services/celigo/sanitizer.py module
# docstring, FIX ROUNDS 1-2): a live probe found Celigo ignores BOTH
# `include` and `exclude` for payload-bearing fields -- `exclude=mockResponse`
# on GET /v1/imports/{id} still returned mockResponse; a positive `include=`
# allowlist on GET /v1/exports/{id} still returned mockOutput/rawData
# unrequested. Projection is NOT a privacy control in either direction.
# sanitize() is the ONLY effective control. Consequently:
#   * Tests below assert `include`/`exclude` reach the query string (cheap,
#     real, costs nothing) -- NEVER that they remove a payload field.
#   * Tests below assert sanitize() strips leaked fields regardless of what
#     projection was requested -- this is the actual guarantee callers get.
# ============================================================================


def _json_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestListResourceUnknownKind:
    @pytest.mark.asyncio
    async def test_rejects_a_kind_with_no_known_endpoint(self):
        with pytest.raises(ValueError):
            async for _ in list_resource("bogus", token="tok"):
                pass


class TestGetResourceUnknownKind:
    @pytest.mark.asyncio
    async def test_rejects_a_kind_with_no_known_endpoint(self):
        with pytest.raises(ValueError):
            await get_resource("bogus", "id1", token="tok")


class TestListResourcePagination:
    """Celigo's documented pagination (developer.celigo.com/api/using-the-api/
    pagination.md) is Link-header based: follow `rel="next"` until it's
    absent; a 204 with no body means there is nothing to list. The client
    must not hand-craft `after`/`limit` -- it must follow the exact URL the
    server hands back."""

    @pytest.mark.asyncio
    async def test_follows_link_header_across_pages_then_stops(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json=[{"_id": "f1", "name": "Flow One"}],
                    headers={"Link": '<https://api.integrator.io/v1/flows?after=page2>; rel="next"'},
                )
            return httpx.Response(200, json=[{"_id": "f2", "name": "Flow Two"}])

        async with _json_client(handler) as c:
            results = [item async for item in list_resource("flow", token="tok", client=c)]

        assert [r["_id"] for r in results] == ["f1", "f2"]
        assert len(calls) == 2
        assert "after=page2" in calls[1]

    @pytest.mark.asyncio
    async def test_no_link_header_means_exactly_one_page(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=[{"_id": "f1", "name": "Flow One"}])

        async with _json_client(handler) as c:
            results = [item async for item in list_resource("flow", token="tok", client=c)]

        assert len(results) == 1
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_204_yields_nothing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with _json_client(handler) as c:
            results = [item async for item in list_resource("flow", token="tok", client=c)]

        assert results == []


class TestListResourcePageCap:
    """FIX ROUND 2 (team lead, confirmed by execution 2026-08-27): a
    self-referential Link header (`rel="next"` pointing back at a URL that
    returns the same `rel="next"` header again) drove `list_resource` to
    501+ requests with no termination -- unlike its sibling
    `list_flow_errors_for_step`, which already had `_MAX_ERROR_PAGES`. Task
    5's natural usage (`[item async for item in list_resource(...)]`) drains
    eagerly, so an unbounded loop is a real availability risk, not a
    theoretical one: a malformed or looping Link header would hang the sync
    worker indefinitely.

    Fix: `list_resource` now stops following `rel="next"` after
    `_MAX_LIST_PAGES` pages and RAISES `CeligoError` rather than silently
    yielding a truncated list -- silently truncating a resource listing is
    the same silent-wrong-answer class as the `numOpenError` zero fixed in
    FIX ROUND 1: a caller that sees an exception knows the sync is
    incomplete, a caller that sees a short list does not.
    """

    @pytest.mark.asyncio
    async def test_a_looping_link_header_raises_instead_of_spinning_forever(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            # A hard breaker INDEPENDENT of the cap under test: if
            # list_resource's own cap is broken, this fails the test fast
            # with a clear assertion instead of spinning for minutes or
            # hanging the suite on a timeout -- exactly what the team lead
            # asked for instead of relying on a timeout.
            if len(calls) > 2 * _MAX_LIST_PAGES:
                raise AssertionError(
                    f"list_resource made over {2 * _MAX_LIST_PAGES} requests -- its page cap did not fire"
                )
            return httpx.Response(
                200,
                json=[{"_id": f"f{len(calls)}", "name": "Flow"}],
                headers={"Link": '<https://api.integrator.io/v1/flows?after=loop>; rel="next"'},
            )

        async with _json_client(handler) as c:
            with pytest.raises(CeligoError):
                async for _ in list_resource("flow", token="tok", client=c):
                    pass

        # Confirms the cap is what stopped it, not the hard breaker above --
        # if this were ever >= 2 * _MAX_LIST_PAGES the AssertionError branch
        # would have fired instead of CeligoError.
        assert len(calls) <= _MAX_LIST_PAGES


class TestProjectionParamsReachTheWire:
    """Assert ONLY that `include`/`exclude` reach the query string -- never
    that they remove anything. See module-level note above."""

    @pytest.mark.asyncio
    async def test_include_reaches_query_string_on_list(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        async with _json_client(handler) as c:
            async for _ in list_resource("flow", token="tok", include=["name", "disabled"], client=c):
                pass

        assert seen["params"]["include"] == "name,disabled"

    @pytest.mark.asyncio
    async def test_exclude_reaches_query_string_on_get(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"_id": "i1", "name": "Import One"})

        async with _json_client(handler) as c:
            await get_resource("import", "i1", token="tok", exclude="mockResponse,rawData", client=c)

        assert seen["params"]["exclude"] == "mockResponse,rawData"


class TestSanitizationIsTheOnlyControl:
    """The load-bearing regression test: simulate exactly the falsified
    scenario (exclude requested, Celigo ignores it for a payload field
    anyway) and prove the fetcher's OUTPUT is still clean -- because
    sanitize() ran, not because exclude worked."""

    @pytest.mark.asyncio
    async def test_get_resource_strips_a_leaked_payload_field_despite_exclude(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # Celigo returns mockResponse anyway, exactly as observed live,
            # even though exclude=mockResponse was requested.
            return httpx.Response(
                200,
                json={"_id": "i1", "name": "Import One", "mockResponse": {"_headers": {"set-cookie": "leak"}}},
            )

        async with _json_client(handler) as c:
            result = await get_resource("import", "i1", token="tok", exclude="mockResponse", client=c)

        assert "mockResponse" not in result
        assert result == {"_id": "i1", "name": "Import One"}

    @pytest.mark.asyncio
    async def test_list_resource_sanitizes_every_item(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"_id": "e1", "name": "Export One", "rawData": {"leak": True}}])

        async with _json_client(handler) as c:
            results = [item async for item in list_resource("export", token="tok", client=c)]

        assert "rawData" not in results[0]

    @pytest.mark.asyncio
    async def test_list_flow_errors_for_step_sanitizes_each_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"errors": [{"errorId": "e1", "message": "boom", "unallowlistedField": "leak"}]},
            )

        async with _json_client(handler) as c:
            errors = await list_flow_errors_for_step("flow1", "step1", token="tok", client=c)

        assert "unallowlistedField" not in errors[0]
        assert errors[0]["errorId"] == "e1"


class TestRetryAfterIsHonoured:
    @pytest.mark.asyncio
    async def test_429_with_retry_after_is_retried_and_then_succeeds(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"_id": "f1", "name": "Flow One"})

        async with _json_client(handler) as c:
            result = await get_resource("flow", "f1", token="tok", client=c)

        assert len(calls) == 2
        assert result["_id"] == "f1"

    @pytest.mark.asyncio
    async def test_429_exhausting_retries_raises_celigo_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0"})

        async with _json_client(handler) as c:
            with pytest.raises(CeligoError):
                await get_resource("flow", "f1", token="tok", client=c)

    @pytest.mark.asyncio
    async def test_429_is_honoured_during_pagination_too(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json=[{"_id": "f1", "name": "Flow One"}])

        async with _json_client(handler) as c:
            results = [item async for item in list_resource("flow", token="tok", client=c)]

        assert len(results) == 1
        assert len(calls) == 2


class TestNewFetchersAuthErrors:
    @pytest.mark.asyncio
    async def test_get_resource_401_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid token"})

        async with _json_client(handler) as c:
            with pytest.raises(CeligoAuthError):
                await get_resource("flow", "f1", token="bad", client=c)

    @pytest.mark.asyncio
    async def test_list_resource_401_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid token"})

        async with _json_client(handler) as c:
            with pytest.raises(CeligoAuthError):
                async for _ in list_resource("flow", token="bad", client=c):
                    pass


class TestFlowErrorsForStep:
    """Live-probed finding (observed-shapes.md, 2026-08-27): errors are
    listed PER STEP. `_id` (flow) alone returns `steps: []` even when errors
    exist -- the useless mode. `list_flow_errors_for_step` makes that mode
    structurally unreachable by requiring both `flow_id` and `step_id` as
    required positional parameters, not optional ones."""

    def test_flow_id_and_step_id_are_required_not_optional(self):
        sig = inspect.signature(list_flow_errors_for_step)
        names = list(sig.parameters)
        assert names[0] == "flow_id"
        assert names[1] == "step_id"
        assert sig.parameters["flow_id"].default is inspect.Parameter.empty
        assert sig.parameters["step_id"].default is inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_request_path_is_flow_step_errors_with_no_query(self):
        """Verified live 2026-09-03: `GET /v1/flows/{flowId}/errors` is the
        PER-FLOW SUMMARY (ignores `_stepId` entirely -- same response either
        way). The OPEN errors of one resource live at
        `GET /v1/flows/{flowId}/{resourceId}/errors` -- no `_stepId` query
        param at all. This test is what proved the old `?_stepId=` request
        was the bug: run against the pre-fix code, it asserted
        `seen["path"] == "/v1/flows/flow1/errors"` and went GREEN on the
        WRONG endpoint -- there was no test pinning the per-resource path
        until this one."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["query"] = request.url.query
            return httpx.Response(200, json={"errors": []})

        async with _json_client(handler) as c:
            await list_flow_errors_for_step("flow1", "step1", token="tok", client=c)

        assert seen["path"] == "/v1/flows/flow1/step1/errors"
        assert seen["query"] == b""

    @pytest.mark.asyncio
    async def test_paginates_via_next_page_url_until_absent(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={
                        "errors": [{"errorId": "e1", "message": "first"}],
                        "nextPageURL": "/v1/flows/flow1/errors?after=abc123",
                    },
                )
            return httpx.Response(200, json={"errors": [{"errorId": "e2", "message": "second"}]})

        async with _json_client(handler) as c:
            errors = await list_flow_errors_for_step("flow1", "step1", token="tok", client=c)

        assert [e["errorId"] for e in errors] == ["e1", "e2"]
        assert len(calls) == 2
        assert "after=abc123" in calls[1]

    @pytest.mark.asyncio
    async def test_returns_a_materialized_list_not_a_generator(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errors": []})

        async with _json_client(handler) as c:
            errors = await list_flow_errors_for_step("flow1", "step1", token="tok", client=c)

        assert isinstance(errors, list)


class TestFlowErrorsPageCap:
    """WHOLE-BRANCH REVIEW FINDING 4 (2026-08-27, PROVEN by execution): unlike
    `list_resource` (`TestListResourcePageCap` above), `list_flow_errors_for_step`
    used to stop SILENTLY at `_MAX_ERROR_PAGES` and return whatever it had
    collected -- no exception. `errors.upsert_errors` then treats every
    previously-open error absent from that truncated list as RESOLVED, and
    the sync completes "successfully", advancing the freshness cursor. The
    sibling fetcher raises for the identical condition (FIX ROUND 2); this
    one must too, for the same reason: a caller that sees an exception knows
    the sync is incomplete, a caller that sees a short list does not.
    """

    @pytest.mark.asyncio
    async def test_a_looping_next_page_url_raises_instead_of_truncating_silently(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            # Same hard breaker as TestListResourcePageCap's sibling test --
            # fails fast with a clear assertion if the cap doesn't fire,
            # instead of spinning or hanging the suite on a timeout.
            if len(calls) > 2 * _MAX_ERROR_PAGES:
                raise AssertionError(
                    f"list_flow_errors_for_step made over {2 * _MAX_ERROR_PAGES} requests -- its page cap did not fire"
                )
            return httpx.Response(
                200,
                json={
                    "errors": [{"errorId": f"e{len(calls)}", "message": "boom"}],
                    # Self-referential -- Celigo (or a malformed proxy) keeps
                    # claiming there's a next page forever.
                    "nextPageURL": "/v1/flows/flow1/errors?after=loop",
                },
            )

        async with _json_client(handler) as c:
            with pytest.raises(CeligoError):
                await list_flow_errors_for_step("flow1", "step1", token="tok", client=c)

        # Confirms the cap is what stopped it, not the hard breaker above.
        assert len(calls) <= _MAX_ERROR_PAGES

    @pytest.mark.asyncio
    async def test_the_truncation_raise_is_its_own_type_and_carries_what_it_did_collect(self):
        """FIX ROUND 9 (scoped re-review R1b). Raising was right, but a bare
        `CeligoError` gave the orchestrator only two options: abort the whole
        connection sync, or catch every Celigo failure alike (auth,
        unparseable body, 5xx) and call them all "partial". A dedicated
        subclass makes the ONE recoverable condition -- "this step's listing
        is truncated" -- distinguishable, and carrying the pages already
        collected lets the caller still RECORD those errors while refusing to
        resolve anything from an admittedly-partial list. That is the
        legitimate caller `upsert_errors(raw_errors_is_complete=False)` never
        had."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) > 2 * _MAX_ERROR_PAGES:
                raise AssertionError(
                    f"list_flow_errors_for_step made over {2 * _MAX_ERROR_PAGES} requests -- its page cap did not fire"
                )
            return httpx.Response(
                200,
                json={
                    "errors": [{"errorId": f"e{len(calls)}", "message": "boom"}],
                    "nextPageURL": "/v1/flows/flow1/errors?after=loop",
                },
            )

        async with _json_client(handler) as c:
            with pytest.raises(CeligoIncompleteListingError) as excinfo:
                await list_flow_errors_for_step("flow1", "step1", token="tok", client=c)

        # Still a CeligoError: an older caller that only knows the base type
        # keeps its existing fail-closed behaviour.
        assert isinstance(excinfo.value, CeligoError)
        # Every error from the pages that DID come back, sanitized, in order.
        partial = excinfo.value.partial_errors
        assert len(partial) == _MAX_ERROR_PAGES
        assert partial[0]["errorId"] == "e1"


class TestFlowErrorSummary:
    """`list_flow_error_summary` -- `GET /v1/flows/{flowId}/errors`, verified
    live 2026-09-03: `{"flowErrors": [{"_expOrImpId": ..., "numError": ...},
    ...]}`, one entry per export/import in the flow. No query params (the
    endpoint ignores `_stepId` regardless -- that's the bug this fixes; see
    `TestFlowErrorsForStep`'s new path test for the per-resource sibling)."""

    @pytest.mark.asyncio
    async def test_parses_the_summary_shape_and_ignores_malformed_entries(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "flowErrors": [
                        {"_expOrImpId": "exp_1", "numError": 0},
                        {"_expOrImpId": "imp_1", "numError": 7},
                        # Malformed -- missing _expOrImpId, or a non-numeric
                        # numError, or not even a dict. All three are skipped,
                        # never given a fabricated key or a guessed count.
                        {"numError": 3},
                        {"_expOrImpId": "bad_count", "numError": "not-a-number"},
                        "not-a-dict",
                    ]
                },
            )

        async with _json_client(handler) as c:
            counts = await list_flow_error_summary("flow1", token="tok", client=c)

        assert counts == {"exp_1": 0, "imp_1": 7}

    @pytest.mark.asyncio
    async def test_request_has_no_query_params(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["query"] = request.url.query
            return httpx.Response(200, json={"flowErrors": []})

        async with _json_client(handler) as c:
            await list_flow_error_summary("flow1", token="tok", client=c)

        assert seen["path"] == "/v1/flows/flow1/errors"
        assert seen["query"] == b""

    @pytest.mark.asyncio
    async def test_204_returns_empty_dict(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with _json_client(handler) as c:
            counts = await list_flow_error_summary("flow1", token="tok", client=c)

        assert counts == {}

    @pytest.mark.asyncio
    async def test_unparseable_body_raises_celigo_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        async with _json_client(handler) as c:
            with pytest.raises(CeligoError):
                await list_flow_error_summary("flow1", token="tok", client=c)

    @pytest.mark.asyncio
    async def test_non_dict_body_raises_celigo_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"flowErrors": []}])

        async with _json_client(handler) as c:
            with pytest.raises(CeligoError):
                await list_flow_error_summary("flow1", token="tok", client=c)

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid token"})

        async with _json_client(handler) as c:
            with pytest.raises(CeligoAuthError):
                await list_flow_error_summary("flow1", token="bad", client=c)


class TestErrorSummaryForIntegration:
    """FIX ROUND 1 (team lead, live-verified 2026-08-27): two live calls
    against the same three real flows, same order --
    `list_flows(limit=3, includeErrorCounts=true)` put `"numOpenError": 0` on
    every item; `list_flows(limit=3)` (plain) had NO `numOpenError` key at
    all, on any item. So a plain `GET /v1/flows?_integrationId=` -- the only
    primitive this function had confirmed evidence for -- can NEVER carry a
    real error count; summing an always-absent field always produces 0,
    indistinguishable from "no errors" for an account that currently has
    103+ open errors across 13+ flows. A monitoring feature that silently
    reports zero problems is worse than one that reports nothing.

    THE FIX: this function now refuses to guess. It raises `CeligoError`
    unconditionally, before making any request, rather than compute and
    return a number that looks like a real count but isn't. There is no
    verified REST primitive today that can honestly answer "how many open
    errors does this integration have" -- the only known populating path,
    `POST /v1/flows/runs/stats`, is undocumented and was never confirmed.
    Callers need either that endpoint (once verified) or a per-flow,
    per-step fan-out through `list_flow_errors_for_step`.
    """

    @pytest.mark.asyncio
    async def test_raises_even_given_a_client_that_would_otherwise_answer_and_makes_no_request(self):
        """The strong form: even wired to a client that would return a
        plausible-looking (but numOpenError-absent, per the live-verified
        finding) flow listing, this must still raise -- proving the refusal
        is unconditional -- and it must not spend a network call finding
        that out."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=[{"_id": "f1", "name": "Flow One"}])

        async with _json_client(handler) as c:
            with pytest.raises(CeligoError):
                await list_error_summary_for_integration("integ1", token="tok", client=c)

        assert calls == []
