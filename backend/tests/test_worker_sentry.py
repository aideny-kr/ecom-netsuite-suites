"""Sentry reaches the Celery workers, not only the FastAPI process.

Before this, sentry_sdk.init lived only inside the API lifespan, so an unattended
worker exception was visible nowhere but the job row.
"""

import sys
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock

from celery.signals import worker_init

from app.core.config import settings
from app.workers import celery_app as mod


def _fake_sentry(monkeypatch):
    fake = SimpleNamespace(init=MagicMock())
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    return fake


def test_worker_init_initialises_sentry_with_the_configured_dsn(monkeypatch):
    fake = _fake_sentry(monkeypatch)
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://key@o1.ingest.sentry.io/1")

    mod.init_worker_observability(sender=None)

    fake.init.assert_called_once()
    assert fake.init.call_args.kwargs["dsn"] == "https://key@o1.ingest.sentry.io/1"
    assert fake.init.call_args.kwargs["environment"] == settings.APP_ENV
    assert fake.init.call_args.kwargs["send_default_pii"] is False


def test_worker_init_without_a_dsn_is_a_noop(monkeypatch):
    fake = _fake_sentry(monkeypatch)
    monkeypatch.setattr(settings, "SENTRY_DSN", "")

    mod.init_worker_observability(sender=None)

    fake.init.assert_not_called()


def test_handler_is_connected_to_the_worker_init_signal_and_shared_with_the_api():
    def receiver(entry):
        receiver = entry[1]
        return receiver() if isinstance(receiver, weakref.ReferenceType) else receiver

    names = {getattr(receiver(entry), "__name__", None) for entry in worker_init.receivers}
    assert "init_worker_observability" in names

    from app.core.observability import init_sentry
    from app.main import _init_sentry

    assert _init_sentry is init_sentry
