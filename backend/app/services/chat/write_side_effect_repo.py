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

import re
import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.write_side_effect import WriteSideEffect
from app.services.chat.write_side_effect import SideEffectStatus, classify_retry_result

__all__ = ["record_attempt", "settle_from_result", "unsettled_for_tenant"]

# A bare SQL identifier. NetSuite record types are `customer`, `salesOrder`,
# `customrecord_ecom_config` — never quoted, never spaced, never punctuated.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
    # ATOMIC UPSERT, not select-then-insert. The old version checked for a row
    # and then inserted — a check-then-act race against
    # UNIQUE(tenant_id, idempotency_key). Two concurrent attempts on one key
    # both saw nothing, both inserted, and the loser's IntegrityError was
    # swallowed upstream, so its write proceeded with NO log at all: exactly
    # the guarantee this module exists to provide, silently absent. The
    # docstring claimed "Upserts" the whole time.
    #
    # DO UPDATE is deliberately guarded on `status = 'attempted'`: re-attempting
    # work must refresh a still-open row's payload, but must NEVER reset a
    # SETTLED row — once NetSuite has told us the answer, nothing may erase it.
    stmt = (
        pg_insert(WriteSideEffect.__table__)
        .values(
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
        .on_conflict_do_update(
            constraint="uq_write_side_effect_tenant_key",
            set_={
                "payload_json": payload,
                # Core-level pg_insert bypasses the ORM unit of work, so
                # TimestampMixin's onupdate never fires — an upserted row would
                # keep its original updated_at and look untouched.
                "updated_at": func.now(),
                # A caller-supplied externalId can legitimately be re-sent to a
                # DIFFERENT connector (our own keys can't — connector_id
                # participates in them). Refresh it, or the row would keep
                # pointing at the account the FIRST attempt used, which is the
                # one value reconciliation cannot afford to have wrong.
                "connector_id": connector_id,
            },
            where=WriteSideEffect.__table__.c.status == SideEffectStatus.ATTEMPTED.value,
        )
    )
    await db.execute(stmt)
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
    status = classify_retry_result(raw_result, idempotency_key=idempotency_key)

    last = (raw_result or "")[:8000]
    if status is SideEffectStatus.ATTEMPTED:
        # No definite answer. Record WHAT we could not read — that text is what
        # a human resolving the row has to work from — but leave the status
        # exactly where it is.
        await db.execute(
            update(WriteSideEffect)
            .where(
                WriteSideEffect.tenant_id == tenant_id,
                WriteSideEffect.idempotency_key == idempotency_key,
            )
            .values(last_result=last, updated_at=func.now())
        )
        return status

    record_id = None
    if status is SideEffectStatus.WRITTEN:
        import json

        try:
            parsed = json.loads(raw_result)
            rid = parsed.get("recordId") or parsed.get("id") or parsed.get("internalId")
            record_id = str(rid) if rid else None
        except Exception:
            # A duplicate-refusal carries no id. The row is still 'written' —
            # reconciliation can recover the id by externalId when needed.
            record_id = None

    values: dict[str, Any] = {"status": status.value, "last_result": last, "updated_at": func.now()}
    if record_id is not None:
        values["netsuite_record_id"] = record_id

    # ONE guarded UPDATE. The `status = 'attempted'` predicate lives in the
    # WHERE clause so the DATABASE decides who wins — a definite answer is
    # final, and a late or replayed settlement cannot rewrite it. Doing this as
    # SELECT-check-mutate in Python let two settlements on separate sessions
    # both read 'attempted' and both proceed; since settlement now runs on an
    # ISOLATED session, that race is an ordinary path rather than a theoretical
    # one.
    result = await db.execute(
        update(WriteSideEffect)
        .where(
            WriteSideEffect.tenant_id == tenant_id,
            WriteSideEffect.idempotency_key == idempotency_key,
            WriteSideEffect.status == SideEffectStatus.ATTEMPTED.value,
        )
        .values(**values)
    )
    if result.rowcount == 0:
        # Either no such row, or it was already settled. Report what the row
        # actually says rather than what this late message claimed.
        existing = (
            await db.execute(
                select(WriteSideEffect.status).where(
                    WriteSideEffect.tenant_id == tenant_id,
                    WriteSideEffect.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        return SideEffectStatus(existing) if existing else status
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
    # The question is only answerable if the key was actually SENT. A key we
    # derived locally and did not put in the payload cannot be found in
    # NetSuite whether or not the write landed, so an empty result says
    # nothing — and reading it as "never landed, safe to retry" is precisely
    # the proxy-predicate defect this table exists to end. Checked against
    # payload_json, which IS what we sent, so a caller cannot get this wrong
    # by forgetting to stamp: the row simply stays unsettled for a human.
    _sent = row.payload_json or {}
    if _sent.get("externalId") != row.idempotency_key and _sent.get("externalid") != row.idempotency_key:
        return SideEffectStatus.ATTEMPTED

    # `record_type` lands in the FROM clause, where no bind parameter can go —
    # SQL identifiers are not parameterizable — and it is MODEL/TOOL-supplied,
    # not ours. So it is validated as an identifier rather than escaped:
    # anything outside [A-Za-z_][A-Za-z0-9_]* cannot be a NetSuite record type
    # and is refused. Fails CLOSED — the row stays unsettled for a human,
    # because the alternative to asking safely is "do not ask", never "ask
    # unsafely" against the customer's own account.
    if not _IDENTIFIER_RE.fullmatch(row.record_type or ""):
        return SideEffectStatus.ATTEMPTED

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


def _resolve_factory(session_factory: Any) -> Any:
    if session_factory is not None:
        return session_factory
    from app.core.database import async_session_factory

    return async_session_factory


async def record_attempt_isolated(*, session_factory: Any = None, **kwargs: Any) -> str | None:
    """``record_attempt`` on its OWN session and transaction.

    The side-effect log must share NO transactional fate with the chat turn.
    Doing it on the caller's session was wrong three ways, and the third only
    showed up when the failure path was finally executed:

    1. Committing the caller's session mid-turn commits whatever else that
       session had pending — the log has no business deciding that.
    2. A failure left the caller's session needing a rollback, so the next
       statement raised ``PendingRollbackError`` and killed a write the human
       had already approved (T2 gate round 1, five findings).
    3. Rolling back to fix (2) EXPIRES every ORM object in the session, so the
       next attribute access (``session.id``) lazy-loads outside the greenlet
       and raises ``MissingGreenlet``. The fix for the poisoned session
       poisoned the session differently.

    A separate session removes the shared fate rather than managing it: this
    transaction can fail, roll back, and be discarded without the caller ever
    observing it. It also makes the "committed BEFORE the call" guarantee
    honest — an independent transaction, not a commit entangled with the turn.

    Returns the key, or ``None`` if the log could not be written. Never raises:
    a logging failure must not block an approved write.
    """
    tenant_id = kwargs.get("tenant_id")
    try:
        from app.core.database import set_tenant_context

        async with _resolve_factory(session_factory)() as se_db:
            await set_tenant_context(se_db, str(tenant_id))
            key = await record_attempt(se_db, **kwargs)
            await se_db.commit()
            return key
    except Exception:
        import logging

        logging.getLogger(__name__).warning("side-effect attempt not recorded", exc_info=True)
        return None


async def settle_from_result_isolated(
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    raw_result: str,
    session_factory: Any = None,
) -> SideEffectStatus:
    """``settle_from_result`` on its own session. Same reasoning as above, and
    it matters more here: NetSuite has already acted by this point, so a
    bookkeeping failure must not be able to crash the turn that reports it."""
    try:
        from app.core.database import set_tenant_context

        async with _resolve_factory(session_factory)() as se_db:
            await set_tenant_context(se_db, str(tenant_id))
            status = await settle_from_result(
                se_db, tenant_id=tenant_id, idempotency_key=idempotency_key, raw_result=raw_result
            )
            await se_db.commit()
            return status
    except Exception:
        import logging

        logging.getLogger(__name__).warning("side-effect row not settled", exc_info=True)
        return SideEffectStatus.ATTEMPTED
