"""Shared close-scope predicates — ONE source of truth for what a period close touches.

POST /close/{period} closes EVERY completed run whose date range falls inside the
month, and GET /close-readiness/{period} is the FE CloseChecklist's gate for that
mutation. Both MUST select the same runs and the same left-for-review rows, or the
gate authorizes a close it never inspected (R3-A: a per-run readiness gate once
authorized the per-period close — another run's suggested/material/pending rows
froze behind the closed-run approve guard without ever passing a checklist).

These helpers replace the old keep-in-sync comments between ``close_period()`` and
the readiness counts. They key ONLY on the authoritative ``status``/``bucket`` —
never the advisory confidence composite (the R2 decoupling pattern).
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date

from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_NEEDS_REVIEW,
    bucket_conditions,
)


def parse_period(period: str) -> tuple[date, date]:
    """Parse ``'YYYY-MM'`` into ``(first_day, last_day)`` of the month.

    Raises ``ValueError`` on anything else (bad shape, non-numeric, month
    outside 1..12 — ``calendar.IllegalMonthError`` subclasses ``ValueError``).
    Callers translate that into the canonical 400 "Period must be YYYY-MM format".
    """
    year_str, month_str = period.split("-")
    year, month = int(year_str), int(month_str)
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return first_day, last_day


def closeable_runs_conditions(tenant_id: uuid.UUID, period: str):
    """Conditions selecting exactly the runs ``close_period(period)`` will close.

    Tenant-scoped; a run is in scope iff its full date range falls inside the
    month AND it is ``completed`` (running/failed/already-closed runs are never
    touched by close). Raises ``ValueError`` for a malformed period.
    """
    first_day, last_day = parse_period(period)
    return (
        ReconciliationRun.tenant_id == tenant_id,
        ReconciliationRun.date_from >= first_day,
        ReconciliationRun.date_to <= last_day,
        ReconciliationRun.status == "completed",
    )


def left_for_review_conditions():
    """Conditions selecting the rows close deliberately leaves UNLOCKED.

    status='auto_matched' AND bucket='needs_review': a confident match with a
    MATERIAL variance that no human reviewed — locking it would silently bury
    the discrepancy (HITL). ``close_period()`` skips these (the complement of
    its lock predicate) and the readiness endpoint counts them, both via this
    helper.
    """
    return (
        ReconciliationResult.status == "auto_matched",
        bucket_conditions(BUCKET_NEEDS_REVIEW),
    )
