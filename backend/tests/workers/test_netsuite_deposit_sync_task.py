"""Nightly deposit-sync task honesty.

2026-07-29 incident: the Framework NetSuite connection silently flipped to
`error` (OAuth token expired). ``sync_netsuite_deposits`` returns a
``DepositSyncResult`` whose ``errors`` list carries "No active NetSuite REST
connection found" instead of raising -- the task then returned that dict as
its retval, and InstrumentedTask.on_success recorded the job row
`status='completed'` every night while syncing NOTHING. Four days of mirror
staleness were invisible in job history.

The task must raise when the sync is a TOTAL failure (errors present AND zero
records synced) so InstrumentedTask.on_failure records the job `failed` with
the real reason. Partial success (some records synced despite some row-level
errors) and a fully healthy run must both stay `completed`, with any errors
preserved in the returned summary (which InstrumentedTask.on_success stores
as `result_summary`).
"""

from dataclasses import dataclass, field

import pytest

from app.workers.tasks.netsuite_deposit_sync import (
    NetsuiteDepositSyncFailedError,
    netsuite_deposit_sync,
)


@dataclass
class _FakeResult:
    records_synced: int = 0
    records_new: int = 0
    records_updated: int = 0
    errors: list[str] = field(default_factory=list)


def _patch_sync(monkeypatch, result):
    async def fake_sync_netsuite_deposits(*, db, tenant_id, date_from, date_to):
        return result

    monkeypatch.setattr(
        "app.services.ingestion.netsuite_deposit_sync.sync_netsuite_deposits",
        fake_sync_netsuite_deposits,
    )


def test_no_active_connection_raises_total_failure(monkeypatch):
    """Zero records synced + an error present = total failure -> raise, never
    a silent 'completed' no-op."""
    _patch_sync(
        monkeypatch,
        _FakeResult(errors=["No active NetSuite REST connection found"]),
    )

    with pytest.raises(NetsuiteDepositSyncFailedError, match="No active NetSuite REST connection found"):
        netsuite_deposit_sync(tenant_id="t1", date_from="2026-07-01", date_to="2026-07-02")


def test_partial_success_completes_with_errors_in_summary(monkeypatch):
    """Some records synced despite some row-level errors -> completed, but the
    errors must survive into the returned summary (on_success's result_summary)."""
    _patch_sync(
        monkeypatch,
        _FakeResult(records_synced=3, records_new=2, records_updated=1, errors=["unparseable amount on row 9"]),
    )

    summary = netsuite_deposit_sync(tenant_id="t1", date_from="2026-07-01", date_to="2026-07-02")

    assert summary["records_synced"] == 3
    assert summary["errors"] == ["unparseable amount on row 9"]


def test_healthy_sync_completes(monkeypatch):
    """No errors at all -> completed, empty errors list."""
    _patch_sync(
        monkeypatch,
        _FakeResult(records_synced=5, records_new=2, records_updated=3),
    )

    summary = netsuite_deposit_sync(tenant_id="t1", date_from="2026-07-01", date_to="2026-07-02")

    assert summary["records_synced"] == 5
    assert summary["errors"] == []
