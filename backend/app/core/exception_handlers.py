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


async def _response_validation_handler(request: Request, exc: ResponseValidationError) -> JSONResponse:
    logger.error("response validation failed path=%s errors=%s", request.url.path, exc.errors()[:5])
    return JSONResponse(status_code=500, content={"detail": "Response validation failed", "path": request.url.path})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ResponseValidationError, _response_validation_handler)
