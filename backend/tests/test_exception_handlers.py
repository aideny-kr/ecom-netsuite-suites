"""A ResponseValidationError used to escape FastAPI's ExceptionMiddleware and reach
ServerErrorMiddleware, i.e. OUTSIDE CORSMiddleware: the browser saw a 500 with no
CORS headers and reported "Failed to fetch" -- the 2026-09-01 "0 flows" incident.
The handler turns it into a JSON 500 inside the middleware stack, so headers survive."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.exception_handlers import register_exception_handlers


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
