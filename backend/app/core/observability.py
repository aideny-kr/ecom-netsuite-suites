"""Process-level observability shared by the API and the Celery workers.

One initialiser, imported by ``app.main`` (lifespan) and ``app.workers.celery_app``
(``worker_init`` signal), so an unattended worker exception reaches the same place an
API exception does instead of surfacing only in a job row.
"""

from app.core.config import settings


def init_sentry() -> None:
    """Initialise Sentry when a DSN is configured; a no-op otherwise. Never raises."""
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
        pass  # Sentry is optional; a broken DSN must not stop the app or a worker.
