"""A ResponseValidationError used to escape FastAPI's ExceptionMiddleware and reach
ServerErrorMiddleware, i.e. OUTSIDE CORSMiddleware: the browser saw a 500 with no
CORS headers and reported "Failed to fetch" -- the 2026-09-01 "0 flows" incident.
The handler turns it into a JSON 500 inside the middleware stack, so headers survive."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.exception_handlers import register_exception_handlers

HANDLER_LOGGER = "app.core.exception_handlers"


class Out(BaseModel):
    n: int


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["http://example.test"], allow_methods=["*"], allow_headers=["*"])
    register_exception_handlers(app)

    @app.get("/bad", response_model=Out)
    async def bad():
        return {"n": "not-an-int"}

    return app


async def test_response_validation_error_is_a_json_500_with_cors_headers():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/bad", headers={"Origin": "http://example.test"})
    assert r.status_code == 500
    assert r.json()["detail"] == "Response validation failed"
    assert r.headers.get("access-control-allow-origin") == "http://example.test"


async def test_the_log_line_never_carries_the_offending_input_value(caplog):
    """Final-review finding C1. `ResponseValidationError.errors()` dicts carry an
    `input` key holding the RAW value that failed to validate -- for this repo's
    own Celigo routes that value is customer error text (`CeligoErrorOut.message`,
    `CeligoErrorSignatureOut.sample_message`), whose model fields are explicitly
    annotated "PII -- never logged". Logging `exc.errors()` verbatim wrote it to
    the application log anyway. The handler must log only the STRUCTURAL half of
    each error (`loc`/`type`/`msg`) and drop `input`/`ctx`/`url`."""
    sentinel = "sentinel-pii-order-4f2a-customer@example.test"

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/leaky", response_model=Out)
    async def leaky():
        return {"n": sentinel}

    with caplog.at_level(logging.ERROR, logger=HANDLER_LOGGER):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/leaky")

    assert r.status_code == 500
    assert sentinel not in r.text, "the RESPONSE must not echo it either"

    records = [rec for rec in caplog.records if rec.name == HANDLER_LOGGER]
    assert records, "the handler must still log the failure"
    logged = "\n".join(rec.getMessage() for rec in records)
    assert sentinel not in logged, f"PII leaked into the log line: {logged}"
    # ...while keeping what makes the line diagnosable at all.
    assert "/leaky" in logged and "'loc'" in logged and "'msg'" in logged
    assert "'input'" not in logged
