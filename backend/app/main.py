from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.middleware import CorrelationIdMiddleware


def _validate_production_secrets() -> None:
    """Refuse to start if production is using default/insecure secrets."""
    if settings.APP_ENV == "development":
        return

    errors = []
    if settings.JWT_SECRET_KEY == "change-me-in-production":
        errors.append("JWT_SECRET_KEY is still the default value")
    if settings.ENCRYPTION_KEY == "change-me-generate-a-real-fernet-key":
        errors.append("ENCRYPTION_KEY is still the default value")
    if errors:
        raise RuntimeError(
            f"FATAL: Insecure configuration for APP_ENV='{settings.APP_ENV}': "
            + "; ".join(errors)
            + ". Set proper secret values before starting."
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if settings.APP_ENV != "development":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def _init_sentry() -> None:
    """Initialize Sentry error tracking if DSN is configured."""
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            environment=settings.APP_ENV,
            send_default_pii=False,
        )
    except Exception:
        pass  # Sentry is optional — don't crash the app if it fails


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_production_secrets()
    _init_sentry()
    setup_logging()
    yield


def create_app() -> FastAPI:
    # Disable Swagger/ReDoc in non-development environments
    is_dev = settings.APP_ENV == "development"

    application = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if is_dev else None,
        redoc_url="/redoc" if is_dev else None,
    )

    # Middleware (last added = outermost = runs first)
    # Order: CORS (outermost) → SecurityHeaders → CorrelationId (innermost)
    application.add_middleware(CorrelationIdMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routes
    application.include_router(api_router)

    return application


app = create_app()
