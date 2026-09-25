# ruff: noqa: F811
"""Durable paid-call reservations with real, separate committed worker sessions."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.services.transaction_ops import hybrid_classification as h
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.runner import run_investigation
from app.services.typesafe.client import JevResult
from tests.test_concurrent_source_pipeline import committed  # noqa: F401
from tests.test_inc_hybrid import report
from tests.test_transaction_chunk_runner import setup


async def prepare(committed, monkeypatch, *, claim=True):
    db, actor, factory = committed
    run, refs, *_ = await setup(db, actor, monkeypatch, size=2)
    await db.commit()
    monkeypatch.setattr(settings, "JEV_TRANSACTION_OPS_CONFIG_IDS", str(run.config_id))
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    monkeypatch.setattr(settings, "JEV_TRANSACTION_OPS_MODE", "live")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "test-never-sent")
    provider = AsyncMock(
        return_value=(
            JevResult({"route": {"choice": "tax_difference", "confidence": 0.95}}, settings.JEV_MODEL, 100, 12, 5),
            None,
        )
    )
    monkeypatch.setattr(h.client, "try_ask", provider)
    token = await state.claim_run(db, actor.tenant_id, run.id) if claim else None
    return db, actor.tenant_id, run.id, token, provider


async def counts(db, tenant):
    return dict(
        (
            await db.execute(
                select(AuditEvent.action, func.count())
                .where(AuditEvent.tenant_id == tenant, AuditEvent.action.in_([h.RESERVED, h.COMPLETED]))
                .group_by(AuditEvent.action)
            )
        ).all()
    )


async def test_paid_once_reverified_on_cache_hit_and_changed_evidence_sends_again(committed, monkeypatch):
    db, tenant, run, token, provider = await prepare(committed, monkeypatch)
    r = report()
    first = await h.classify_report(tenant, run, token, r)
    assert first["status"] == "verified", first
    assert first["input_tokens"] == 100 and first["output_tokens"] == 5
    assert (await h.classify_report(tenant, run, token, r))["cache_hit"] is True
    # Verification runs again even if the same projected facts have a now-invalid status.
    r["balance"]["status"] = "not_verified"
    cached = await h.classify_report(tenant, run, token, r)
    assert cached["cache_hit"] and cached["status"] == "needs_review"
    r["balance"]["amounts"]["tax"]["target"] = "8"
    await h.classify_report(tenant, run, token, r)
    assert provider.await_count == 2
    assert await counts(db, tenant) == {h.RESERVED: 2, h.COMPLETED: 2}


async def test_concurrent_duplicate_and_quota_are_reserved_before_wire(committed, monkeypatch):
    db, tenant, run, token, provider = await prepare(committed, monkeypatch)
    monkeypatch.setattr(h, "MAX_DAILY_CALLS", 1)
    started, release = asyncio.Event(), asyncio.Event()
    response = provider.return_value

    async def held(*args, **kwargs):
        assert await counts(db, tenant) == {h.RESERVED: 1}
        started.set()
        await release.wait()
        return response

    provider.side_effect = held
    task = asyncio.create_task(h.classify_report(tenant, run, token, report()))
    await asyncio.wait_for(started.wait(), 5)
    try:
        duplicate = await h.classify_report(tenant, run, token, report())
        assert duplicate["reason"] == "indeterminate_attempt"
        other = report()
        other["order_reference"] = "SECOND"
        capped = await h.classify_report(tenant, run, token, other)
        assert capped["reason"] == "daily_budget"
    finally:
        release.set()
        await task
    assert provider.await_count == 1


@pytest.mark.parametrize(
    "control", ["off", "global_off", "allowlist", "wrong_tenant", "lease", "disabled", "mapping", "account"]
)
async def test_authorization_fences_prevent_calls(committed, monkeypatch, control):
    db, tenant, run, token, provider = await prepare(committed, monkeypatch)
    if control == "off":
        monkeypatch.setattr(settings, "JEV_TRANSACTION_OPS_MODE", "off")
    elif control == "global_off":
        monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")
    elif control == "allowlist":
        monkeypatch.setattr(settings, "JEV_TRANSACTION_OPS_CONFIG_IDS", "")
    elif control == "wrong_tenant":
        tenant = uuid4()
    elif control == "lease":
        token = uuid4()
    else:
        row = await state.get_run(db, tenant, run)
        config = await state.get_config(db, tenant, row.config_id)
        if control == "disabled":
            config.enabled = False
            await db.commit()
        else:
            # Config evidence is immutable in PostgreSQL. Simulate a historical
            # snapshot mismatch at the read boundary without weakening triggers.
            from types import SimpleNamespace

            current = SimpleNamespace(enabled=True, **{key: getattr(config, key) for key in h._SCOPE})
            if control == "mapping":
                current.mapping_json = {**current.mapping_json, "currency_minor_units": {"USD": 3}}
            else:
                current.netsuite_account_id = "other"
            monkeypatch.setattr(state, "get_config", AsyncMock(return_value=current))
    result = await h.classify_report(tenant, run, token, report())
    assert result is None or result["status"] == "needs_review"
    provider.assert_not_awaited()


@pytest.mark.parametrize("kind", ["outage", "shadow", "disagreement", "low_confidence", "revoked"])
async def test_provider_failures_and_controls_remain_review_only(committed, monkeypatch, kind):
    db, tenant, run, token, provider = await prepare(committed, monkeypatch)
    if kind == "outage":
        provider.return_value = (None, "timeout")
    elif kind == "shadow":
        monkeypatch.setattr(settings, "JEV_TRANSACTION_OPS_MODE", "shadow")
    elif kind in {"disagreement", "low_confidence"}:
        provider.return_value = (
            JevResult(
                {
                    "route": {
                        "choice": "refund_difference" if kind == "disagreement" else "tax_difference",
                        "confidence": 0.5 if kind == "low_confidence" else 1,
                    }
                },
                settings.JEV_MODEL,
                100,
                12,
            ),
            None,
        )
    else:
        response = provider.return_value

        async def revoke(*args, **kwargs):
            row = await state.get_run(db, tenant, run)
            config = await state.get_config(db, tenant, row.config_id)
            config.enabled = False
            await db.commit()
            return response

        provider.side_effect = revoke
    result = await h.classify_report(tenant, run, token, report())
    assert result["route"] == "needs_review" and result["executable"] is False
    await h.classify_report(tenant, run, token, report())
    assert provider.await_count == 1


@pytest.mark.parametrize("origin", ["manual", "schedule"])
async def test_daily_and_manual_runner_store_hybrid_without_changing_financial_result(committed, monkeypatch, origin):
    from app.models.transaction_ops import TransactionFinding

    db, tenant, run_id, _, provider = await prepare(committed, monkeypatch, claim=False)
    provider.return_value = (
        JevResult({"route": {"choice": "missing_order", "confidence": 0.99}}, settings.JEV_MODEL, 100, 12),
        None,
    )
    if origin == "schedule":
        from app.schemas.transaction_runs import RunCreate

        original = await state.get_run(db, tenant, run_id)
        config = await state.get_config(db, tenant, original.config_id)
        config.schedule_enabled = True
        await db.commit()
        daily = await state.create_run(
            db,
            tenant,
            config.id,
            RunCreate(
                origin="schedule",
                evaluation_key="daily-hybrid",
                window_start=original.params_json["window_start"],
                window_end=original.params_json["window_end"],
            ),
        )
        daily.progress_json = {**daily.progress_json, **original.progress_json}
        await db.commit()
        run_id = daily.id
    before = {}
    classify = h.classify_report

    async def captured(tenant, run_id, token, report):
        from copy import deepcopy

        before[report["order_reference"]] = deepcopy(report["balance"])
        return await classify(tenant, run_id, token, report)

    monkeypatch.setattr(h, "classify_report", captured)
    result = await run_investigation(db, tenant, run_id)
    assert result["termination_reason"] == "done", result
    findings = (await db.scalars(select(TransactionFinding).where(TransactionFinding.run_id == run_id))).all()
    assert len(findings) == 2
    for finding in findings:
        assert finding.report_json["balance"] == before[finding.order_reference]
        assert finding.report_json["hybrid_classification"]["provider_called"] is True
    current = await state.get_run(db, tenant, run_id)
    assert current.progress_json["jev_calls"] == 2
    assert current.progress_json["processed"] == 2 and not current.progress_json["pending_refs"]


async def test_cancelled_paid_attempt_is_indeterminate_and_not_sent_again(committed, monkeypatch):
    db, tenant, run, token, provider = await prepare(committed, monkeypatch)
    provider.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await h.classify_report(tenant, run, token, report())
    retry = await h.classify_report(tenant, run, token, report())
    assert retry["reason"] == "indeterminate_attempt"
    assert provider.await_count == 1
    assert await counts(db, tenant) == {h.RESERVED: 1}


async def test_provider_runs_without_database_transaction(committed, monkeypatch):
    db, tenant, run, token, provider = await prepare(committed, monkeypatch)
    original = h._classify
    observed = []

    async def capture(session, *args):
        response = provider.return_value

        async def wire(*args, **kwargs):
            observed.append(session.in_transaction())
            return response

        provider.side_effect = wire
        return await original(session, *args)

    monkeypatch.setattr(h, "_classify", capture)
    await h.classify_report(tenant, run, token, report())
    assert observed == [False]
