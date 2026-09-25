"""Scoped, paid-once Jev judgments over stored Inc exceptions.

The append-only audit ledger is committed before a request. Its tenant advisory
transaction lock serializes cache and daily quota reservations across workers.
A reservation without a result is indeterminate and is never automatically sent
again. The independent verifier runs on the current report, including cache hits.
"""

import asyncio
import hashlib
import json
import time
from datetime import timedelta

import structlog
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.database import set_tenant_context_session, worker_async_session
from app.models.audit import AuditEvent
from app.schemas.transaction_runs import _bounded_json
from app.services import audit_service
from app.services.transaction_ops import hybrid_judgment as judgment
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.source_eligibility import excluded_report
from app.services.typesafe import access, client

logger = structlog.get_logger()
RESERVED = "transaction_ops.jev_reserved"
COMPLETED = "transaction_ops.jev_classified"
MAX_DAILY_CALLS = 500
MAX_REQUEST_BYTES = 8192
# Conservative internal quota, not a statement of the vendor's billed tokens.
# Keep reservations charged even on transport errors or incomplete usage reports.
TOKENS_PER_RESERVATION = 16384
MAX_DAILY_RESERVED_TOKENS = 8_192_000
_SCOPE = (
    "source_connection_id",
    "source_step_id",
    "netsuite_connection_id",
    "netsuite_account_id",
    "subsidiary_id",
    "record_type",
    "mapping_json",
)


def configured(config_id):
    return str(config_id) in {s.strip() for s in settings.JEV_TRANSACTION_OPS_CONFIG_IDS.split(",") if s.strip()}


def eligible(report):
    return not excluded_report(report) and (report.get("balance") or {}).get("status") in {
        "difference",
        "ambiguous",
        "currency_mismatch",
        "missing_in_netsuite",
        "incomplete",
        "not_verified",
    }


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)


def fingerprint(snapshot, report, projected):
    return hashlib.sha256(
        _json(
            {
                "version": judgment.VERSION,
                "model": settings.JEV_MODEL,
                "questions": judgment.QUESTIONS,
                "config_id": snapshot.get("id"),
                "scope": {k: snapshot.get(k) for k in _SCOPE},
                "order_reference": report["order_reference"],
                "source_record_id": (report.get("source") or {}).get("record_id"),
                "target_record_ids": sorted(str(t.get("record_id")) for t in (report.get("targets") or [])),
                "state": projected,
            }
        ).encode()
    ).hexdigest()


async def _authorize(db, tenant, run_id, token):
    now = await state.run_clock(db)
    run = await state.get_run(db, tenant, run_id)
    state._lease(run, token, now)
    if not configured(run.config_id) or run.origin == "recovery" or now >= run.deadline_at:
        return None
    config = await state.get_config(db, tenant, run.config_id)
    snapshot = run.config_snapshot
    if (
        not config.enabled
        or not await state.enabled_for_run(db, tenant)
        or snapshot.get("netsuite_account_id") != "6738075"
        or snapshot.get("subsidiary_id") != "1"
        or _json({k: snapshot.get(k) for k in _SCOPE}) != _json({k: getattr(config, k) for k in _SCOPE})
    ):
        return None
    permission = await access.resolve_access(db, tenant, workflow="transaction_ops")
    return (run, permission, now) if permission else None


def _decision(report, *, answer=None, reason=None, mode="live", cached=False, audit_id=None, **metrics):
    verified = judgment.verify(report, answer, minimum_confidence=settings.JEV_RECON_MIN_CONFIDENCE)
    if reason:
        verified.update(accepted=False, applied_route="needs_review", reason=reason)
    accepted = verified["accepted"] and mode == "live"
    route = verified["applied_route"] if accepted else "needs_review"
    return {
        "version": judgment.VERSION,
        "mode": mode,
        "status": "verified" if accepted else "shadow" if mode == "shadow" else "needs_review",
        "proposed_route": answer.get("choice") if isinstance(answer, dict) else None,
        "confidence": str(answer["confidence"])
        if isinstance(answer, dict) and answer.get("confidence") is not None
        else None,
        "checked_route": verified["checked_route"],
        "route": route,
        "reason": verified["reason"],
        "next_step": judgment.NEXT_STEP[route],
        "executable": False,
        "cache_hit": cached,
        "reservation_id": str(audit_id) if audit_id else None,
        **metrics,
    }


async def classify_report(tenant_id, run_id, lease_token, report):
    """Optional interpretation; failures cannot alter money or stop reconciliation."""
    started = time.monotonic()
    try:
        if not eligible(report):
            return None
        async with worker_async_session(pin_connection=True) as db:
            await set_tenant_context_session(db, str(tenant_id))
            result = await _classify(db, tenant_id, run_id, lease_token, report)
            if result is not None:
                result["total_ms"] = round((time.monotonic() - started) * 1000)
            return result
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("transaction_ops.jev_unavailable", error_type=type(error).__name__)
        return _decision(report, reason="classifier_unavailable", provider_called=None)


def attach(report, decision):
    """Optional advice must never make a previously valid financial report unsavable."""
    for block in (
        decision,
        {
            "status": "needs_review",
            "reason": "evidence_size_limit",
            "reservation_id": decision.get("reservation_id"),
            "executable": False,
        },
    ):
        candidate = {**report, "hybrid_classification": block}
        try:
            _bounded_json(candidate)
            return candidate
        except ValueError:
            continue
    # The full judgment remains in its durable audit receipt.
    return report


async def _receipt(db, tenant, reservation_id, key, payload):
    await audit_service.log_event(
        db,
        tenant_id=tenant,
        category="transaction_ops",
        action=COMPLETED,
        actor_type="system",
        resource_type="jev_reservation",
        resource_id=str(reservation_id),
        correlation_id=key,
        payload=payload,
    )
    await db.commit()


async def _classify(db, tenant, run_id, token, report):
    # Namespace the lock by tenant, so separate opted-in configs cannot each
    # spend the tenant's entire daily quota concurrently.
    lock_key = int.from_bytes(hashlib.sha256(f"inc-jev:{tenant}".encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    authorized = await _authorize(db, tenant, run_id, token)
    if authorized is None:
        await db.rollback()
        return None
    run, permission, now = authorized
    projected = judgment.project(report)
    payload_size = len(
        _json({"state": projected, "questions": judgment.QUESTIONS, "model": settings.JEV_MODEL}).encode()
    )
    if payload_size > MAX_REQUEST_BYTES:
        await db.rollback()
        return _decision(report, reason="input_budget", mode=permission.mode, provider_called=False)
    digest = fingerprint(run.config_snapshot, report, projected)
    key = "inc-jev:" + digest
    previous = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant,
            AuditEvent.action == RESERVED,
            AuditEvent.correlation_id == key,
        )
        .order_by(AuditEvent.timestamp)
        .limit(1)
    )
    if previous:
        completed = await db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant,
                AuditEvent.action == COMPLETED,
                AuditEvent.resource_id == str(previous.id),
                AuditEvent.correlation_id == key,
            )
            .order_by(AuditEvent.timestamp.desc())
            .limit(1)
        )
        receipt = dict(completed.payload or {}) if completed else {}
        reservation_id = previous.id
        await db.rollback()
        return _decision(
            report,
            answer=receipt.get("answer"),
            reason=receipt.get("error") if completed else "indeterminate_attempt",
            mode=permission.mode,
            cached=True,
            audit_id=reservation_id,
            provider_called=False,
            evidence_fingerprint=digest,
        )
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = await db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant,
            AuditEvent.action == RESERVED,
            AuditEvent.timestamp >= midnight,
            AuditEvent.timestamp < midnight + timedelta(days=1),
        )
    )
    if count >= MAX_DAILY_CALLS or (count + 1) * TOKENS_PER_RESERVATION > MAX_DAILY_RESERVED_TOKENS:
        await db.rollback()
        return _decision(report, reason="daily_budget", mode=permission.mode, provider_called=False)
    reservation = await audit_service.log_event(
        db,
        tenant_id=tenant,
        category="transaction_ops",
        action=RESERVED,
        actor_type="system",
        resource_type="transaction_config",
        resource_id=str(run.config_id),
        correlation_id=key,
        payload={
            "run_id": str(run.id),
            "fingerprint": digest,
            "version": judgment.VERSION,
            "model": settings.JEV_MODEL,
            "request_bytes": payload_size,
            "reserved_input_tokens": TOKENS_PER_RESERVATION,
            "mode": permission.mode,
        },
    )
    reservation_id = reservation.id
    await db.commit()  # No request before this durable reservation succeeds.
    # Re-read authorization after the commit; card/config/lease revocation wins.
    try:
        authorized = await _authorize(db, tenant, run_id, token)
    except state.StateError:
        authorized = None
    if authorized is None:
        await _receipt(
            db,
            tenant,
            reservation_id,
            key,
            {
                "answer": None,
                "error": "not_sent_revoked",
                "provider_called": False,
                "version": judgment.VERSION,
                "fingerprint": digest,
            },
        )
        return _decision(report, reason="not_sent_revoked", audit_id=reservation_id, provider_called=False)
    _, permission, _ = authorized
    await db.rollback()  # Never hold a DB transaction across the provider call.
    result, error = await client.try_ask(tenant, projected, judgment.QUESTIONS, api_key=permission.api_key)
    if result and result.model != settings.JEV_MODEL:
        error = "model_mismatch"
    answer = result.answers["route"] if result and not error else None
    verification = judgment.verify(report, answer, minimum_confidence=settings.JEV_RECON_MIN_CONFIDENCE)
    receipt = {
        "provider_called": True,
        "answer": answer,
        "error": error,
        "verification": verification,
        "mode": permission.mode,
        "model": result.model if result else settings.JEV_MODEL,
        "input_tokens": result.input_tokens if result else None,
        "output_tokens": result.output_tokens if result else None,
        "provider_ms": result.elapsed_ms if result else None,
        "version": judgment.VERSION,
        "fingerprint": digest,
    }
    await _receipt(db, tenant, reservation_id, key, receipt)
    authorized = await _authorize(db, tenant, run_id, token)
    if authorized is None:
        return _decision(report, reason="revoked", audit_id=reservation_id, provider_called=True)
    _, permission, _ = authorized
    return _decision(
        report,
        answer=answer,
        reason=error,
        mode=permission.mode,
        audit_id=reservation_id,
        provider_called=True,
        evidence_fingerprint=digest,
        **{k: receipt[k] for k in ("model", "input_tokens", "output_tokens", "provider_ms")},
    )
