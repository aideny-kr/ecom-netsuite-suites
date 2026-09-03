"""Process-wide exception handlers that must run INSIDE the middleware stack.

FastAPI dispatches handlers registered here from ExceptionMiddleware, which sits
under CORSMiddleware, so the response keeps its CORS headers. An unhandled
exception instead reaches ServerErrorMiddleware, above CORS: the browser gets a
header-less 500 and reports a network error, which the UI cannot tell apart from
a real outage (2026-09-01 incident)."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# The only keys of a pydantic error dict this handler is allowed to log.
# DELIBERATELY excludes `input` -- the raw value that failed validation, which
# for this app's own routes is customer data (`CeligoErrorOut.message` and
# `CeligoErrorSignatureOut.sample_message` carry raw Celigo error text and are
# annotated "PII -- never logged"; a NetSuite or Stripe payload would be no
# better). Also excludes `ctx` (a custom validator's ValueError, which quotes
# the value it rejected) and `url` (pydantic's docs link, pure noise here).
_SAFE_ERROR_KEYS = ("loc", "type", "msg")


def _safe_errors(exc: ResponseValidationError) -> list[dict[str, object]]:
    """The structural half of each validation error -- WHERE it failed and WHAT
    rule it broke -- with every value-bearing key dropped.

    Scope, stated exactly: this strips the keys that carry the offending value
    verbatim. `msg` is pydantic's own generated sentence ("Input should be a
    valid integer, ...") and does not echo the value for the built-in types;
    a custom validator that interpolates its input into its own ValueError
    message would still reach the log through `msg`, so a model doing that on
    a PII field is the one case this does not cover."""
    return [{key: err[key] for key in _SAFE_ERROR_KEYS if key in err} for err in exc.errors()[:5]]


async def _response_validation_handler(request: Request, exc: ResponseValidationError) -> JSONResponse:
    logger.error("response validation failed path=%s errors=%s", request.url.path, _safe_errors(exc))
    return JSONResponse(status_code=500, content={"detail": "Response validation failed", "path": request.url.path})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ResponseValidationError, _response_validation_handler)
