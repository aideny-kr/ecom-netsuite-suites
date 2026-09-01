"""Persistence for the side-effect log — the half that survives the process.

``write_side_effect.py`` derives the key and classifies an answer, purely. This
module is what makes the record durable: the row goes in BEFORE the call, and
resume settles it from evidence rather than by retrying blind.

The ordering is the entire point and is easy to lose in a refactor:

    record_attempt()   ← COMMITTED before the tool call
    execute_tool_call()
    settle_from_result()

If the row is written after the call, or in the same uncommitted transaction, a
crash between send and confirm leaves nothing — which is precisely today's
behaviour and the reason sandbox customer 5264348 existed while the app said
the write had failed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text

from app.models.write_side_effect import WriteSideEffect
from app.services.chat.write_side_effect import SideEffectStatus, classify_retry_result

__all__ = ["record_attempt", "settle_from_result", "unsettled_for_tenant"]


async def record_attempt(
    db: Any,
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    record_type: str,
    mutation_type: str,
    payload: dict[str, Any] | None = None,
    connector_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    row_index: int | None = None,
    correlation_id: str | None = None,
    session_id: uuid.UUID | None = None,
) -> str:
    """Record that a write is about to be sent. **Caller must COMMIT before the call.**

    Upserts on ``(tenant_id, idempotency_key)``: a second attempt at the same
    work updates the existing row rather than appending. Two rows would make
    the log imply two side effects where there was one, and a log that lies
    about side effects is worse than no log at all.

    Returns the key, so a caller can thread it straight into the payload.
    """
    existing = (
        await db.execute(
            select(WriteSideEffect).where(
                WriteSideEffect.tenant_id == tenant_id,
                WriteSideEffect.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # A retry of work already recorded. Deliberately does NOT reset a
        # settled status back to 'attempted' — once NetSuite has told us the
        # answer, re-attempting must not erase it.
        if existing.status == SideEffectStatus.ATTEMPTED.value:
            existing.payload_json = payload
        return idempotency_key

    db.add(
        WriteSideEffect(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            connector_id=connector_id,
            record_type=record_type,
            mutation_type=mutation_type,
            status=SideEffectStatus.ATTEMPTED.value,
            batch_id=batch_id,
            row_index=row_index,
            correlation_id=correlation_id,
            session_id=session_id,
            payload_json=payload,
        )
    )
    return idempotency_key


async def settle_from_result(
    db: Any, *, tenant_id: uuid.UUID, idempotency_key: str, raw_result: str
) -> SideEffectStatus:
    """Settle a recorded attempt from what NetSuite actually said.

    Only a DEFINITE answer moves the row off ``attempted``. An indeterminate or
    unreadable result leaves it exactly where it was — that is the state meaning
    "go and look", and collapsing it into success or failure is the defect this
    table exists to end.
    """
    status = classify_retry_result(raw_result)

    row = (
        await db.execute(
            select(WriteSideEffect).where(
                WriteSideEffect.tenant_id == tenant_id,
                WriteSideEffect.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return status

    row.last_result = (raw_result or "")[:8000]
    if status is SideEffectStatus.ATTEMPTED:
        return status  # unchanged on purpose — no definite answer arrived

    row.status = status.value
    if status is SideEffectStatus.WRITTEN:
        import json

        try:
            parsed = json.loads(raw_result)
            rid = parsed.get("recordId") or parsed.get("id") or parsed.get("internalId")
            if rid:
                row.netsuite_record_id = str(rid)
        except Exception:
            # A duplicate-refusal carries no id. The row is still 'written' —
            # reconciliation can recover the id by externalId when needed.
            pass
    return status


async def unsettled_for_tenant(db: Any, *, tenant_id: uuid.UUID, limit: int = 200) -> list[WriteSideEffect]:
    """Rows a crash may have left in flight — the resume worklist.

    These are exactly the writes where we do not know whether NetSuite acted.
    Each must be settled by EVIDENCE (query by externalId) before anything is
    retried; retrying blind is what turns one uncertain row into a duplicate.
    """
    result = await db.execute(
        select(WriteSideEffect)
        .where(
            WriteSideEffect.tenant_id == tenant_id,
            WriteSideEffect.status == SideEffectStatus.ATTEMPTED.value,
        )
        .order_by(WriteSideEffect.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def reconcile_by_external_id(
    db: Any,
    *,
    tenant_id: uuid.UUID,
    row: WriteSideEffect,
    suiteql: Any,
) -> SideEffectStatus:
    """Settle one ``attempted`` row by ASKING NETSUITE, never by retrying.

    ``suiteql`` is an async callable taking a query string — injected so this
    is testable without a live connector, and so the caller owns which
    connector the question is asked of (a write is only reconcilable against
    the account it was sent to).

    Note the column case: the catalog spells it ``externalId`` while SuiteQL
    returns ``externalid``. Getting this wrong is the trap that once made
    required_field_registry miss every real customer create.
    """
    key = row.idempotency_key.replace("'", "''")  # the key is ours, but never interpolate unescaped
    query = f"SELECT id FROM {row.record_type} WHERE externalid = '{key}' FETCH FIRST 1 ROWS ONLY"
    try:
        raw = await suiteql(query)
    except Exception:
        return SideEffectStatus.ATTEMPTED  # still unknown; leave it for a human

    import json

    try:
        data = (json.loads(raw) or {}).get("data") or []
    except Exception:
        return SideEffectStatus.ATTEMPTED

    if data:
        row.status = SideEffectStatus.WRITTEN.value
        found = data[0].get("id")
        if found is not None:
            row.netsuite_record_id = str(found)
        return SideEffectStatus.WRITTEN

    # NetSuite has no such record: the write never landed. Safe to retry — and
    # safe BECAUSE the key is in externalId, so if this conclusion were somehow
    # wrong the retry is refused rather than duplicated.
    row.status = SideEffectStatus.REJECTED.value
    return SideEffectStatus.REJECTED


# Keep `text` imported for callers that pass a raw SQL executor in tests.
_ = text
