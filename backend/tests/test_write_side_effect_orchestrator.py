"""The side-effect log, driven through the REAL orchestrator approve branch.

Why this file exists separately from `test_write_confirm_orchestrator.py`:
that suite drives the same branch against `_make_db()`, a MagicMock whose
`execute` returns the confirmation message for EVERY select-shaped statement.
`record_attempt` issues a select of its own, gets a ChatMessage back where it
expects a WriteSideEffect, and falls into its own best-effort except — so the
mock-db tests pass whether the side-effect log works or not. They cannot fail
on it. Testing this against a real database is not thoroughness, it is the
only way the assertion means anything.

These run against the drill Postgres, which carries the full schema.

The property under test is the one the whole design exists for: a write whose
outcome is UNKNOWN must stay on the resume worklist. Every other case — success,
NetSuite refusal — settles and leaves it.
"""

import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.chat import ChatMessage, ChatSession
from app.services.chat.write_outcome import INDETERMINATE_KEY
from app.services.chat.write_side_effect_repo import unsettled_for_tenant

DRILL_URL = os.environ.get("DRILL_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/idem_drill")
pytestmark = pytest.mark.asyncio

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
_TENANT_ID = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")
_USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


@pytest_asyncio.fixture
async def db():
    """A drill-DB session, AND the app's session factory redirected to it.

    The side-effect log now runs on its OWN session via
    `record_attempt_isolated`, which resolves `app.core.database
    .async_session_factory`. Unpatched, these tests would write to whatever
    DATABASE_URL_DIRECT points at — which in this repo is REMOTE SUPABASE.
    The redirect is in the fixture, not in individual tests, so a new test
    cannot forget it and quietly write to production infrastructure.
    """
    engine = create_async_engine(DRILL_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    with patch("app.core.database.async_session_factory", maker):
        async with maker() as s:
            await s.execute(text("DELETE FROM write_side_effects"))
            await s.execute(text("DELETE FROM chat_messages"))
            await s.execute(text("DELETE FROM chat_sessions"))
            await s.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, created_at, updated_at) "
                    "VALUES (:i,'Drill','drill',now(),now()) ON CONFLICT (id) DO NOTHING"
                ),
                {"i": _TENANT_ID},
            )
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, full_name, hashed_password, is_active, "
                    "created_at, updated_at) "
                    "VALUES (:i,:t,'drill@example.com','Drill User','x',true,now(),now()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"i": _USER_ID, "t": _TENANT_ID},
            )
            await s.commit()
            yield s
    await engine.dispose()


async def _seed_pending_write(db, tool_input: dict) -> tuple[ChatSession, ChatMessage]:
    """A real session + a real pending confirmation card, persisted.

    Persisted rather than mocked because the branch selects the card by id and
    then flag_modified()s it — both need a genuine instrumented instance in a
    genuine session.
    """
    from app.services.chat.write_confirmation_service import build_confirmation_payload

    session = ChatSession(id=uuid.uuid4(), tenant_id=_TENANT_ID, user_id=_USER_ID, title="Drill", session_type="chat")
    db.add(session)
    await db.commit()

    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="customer",
        tool_name=_ext("ns_createRecord"),
        tool_input=tool_input,
        session_id=str(session.id),
    )
    assert payload is not None
    msg = ChatMessage(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={**payload.model_dump(), "status": "pending"},
    )
    db.add(msg)
    await db.commit()
    return session, msg


async def _approve(db, session, msg, tool_result: str) -> list[dict]:
    from app.services.chat.orchestrator import run_chat_turn

    with (
        patch(
            "app.services.chat.orchestrator.execute_tool_call",
            AsyncMock(return_value=tool_result),
        ),
        patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
    ):
        return [
            e
            async for e in run_chat_turn(
                db=db,
                session=session,
                user_message="approve",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "approve", "confirmation_id": str(msg.id)},
            )
        ]


async def test_a_timed_out_write_stays_on_the_worklist(db):
    """THE case. NetSuite may or may not have acted, so the row must remain
    'attempted' — the state that means "go and look". Before this branch the
    same timeout was reported as `failed` and offered for blind retry, which is
    how sandbox customer 5264348 came to exist."""
    session, msg = await _seed_pending_write(
        db, {"recordType": "customer", "data": json.dumps({"companyName": "Drill Timeout Co"})}
    )

    await _approve(
        db,
        session,
        msg,
        json.dumps({"error": "exceeded 60-second timeout limit", INDETERMINATE_KEY: True}),
    )

    pending = await unsettled_for_tenant(db, tenant_id=_TENANT_ID)
    assert len(pending) == 1, "a write with an unknown outcome must survive as unsettled"
    assert pending[0].record_type == "customer"
    assert pending[0].netsuite_record_id is None
    # And the row carries what we could not read, so a human can act on it.
    assert "timeout" in (pending[0].last_result or "")


async def test_a_successful_write_settles_and_leaves_the_worklist(db):
    """The unchanged path. A definite success records the id and stops being
    something anyone has to look at."""
    session, msg = await _seed_pending_write(
        db, {"recordType": "customer", "data": json.dumps({"companyName": "Drill Success Co"})}
    )

    await _approve(db, session, msg, json.dumps({"success": True, "recordId": "5264999"}))

    assert await unsettled_for_tenant(db, tenant_id=_TENANT_ID) == []
    row = (
        await db.execute(
            text("SELECT status, netsuite_record_id FROM write_side_effects WHERE tenant_id = :t"),
            {"t": _TENANT_ID},
        )
    ).first()
    assert (row.status, row.netsuite_record_id) == ("written", "5264999")


async def test_a_netsuite_refusal_settles_too(db):
    """A rejection is an ANSWER — the write demonstrably did not land, so it is
    not a mystery and does not belong on the worklist."""
    session, msg = await _seed_pending_write(
        db, {"recordType": "customer", "data": json.dumps({"companyName": "Drill Rejected Co"})}
    )

    await _approve(db, session, msg, json.dumps({"error": "Please enter value(s) for: Subsidiary."}))

    assert await unsettled_for_tenant(db, tenant_id=_TENANT_ID) == []
    got = (
        await db.execute(text("SELECT status FROM write_side_effects WHERE tenant_id = :t"), {"t": _TENANT_ID})
    ).scalar()
    assert got == "rejected"


async def test_the_row_is_keyed_on_the_externalid_the_write_carries(db):
    """The log is only useful if its key is the one NetSuite enforces.

    The card is stamped at BUILD time (base_agent) so the key is inside what
    the HMAC signs; here the orchestrator must reuse THAT value rather than
    re-derive one. A re-derived key would be a second, divergent identity for
    the same write — reconcilable against nothing.
    """
    from app.services.chat.write_side_effect import payload_with_idempotency_key

    stamped, expected_key = payload_with_idempotency_key(
        {"companyName": "Drill Keyed Co"}, batch_id=None, row_index=None
    )
    session, msg = await _seed_pending_write(db, {"recordType": "customer", "data": json.dumps(stamped)})
    approved = json.loads(msg.structured_output["tool_input"]["data"])
    assert approved["externalId"] == expected_key  # the payload a human approved

    await _approve(db, session, msg, json.dumps({"success": True, "recordId": "1"}))

    key = (
        await db.execute(text("SELECT idempotency_key FROM write_side_effects WHERE tenant_id = :t"), {"t": _TENANT_ID})
    ).scalar()
    assert key == expected_key, "logged key must equal the externalId actually sent"


async def test_an_unstamped_write_is_logged_but_never_falsely_settled(db):
    """A card built by a path that does not stamp still gets a row — a write
    went out and that must be recorded. But its key was never sent, so nothing
    may conclude anything from asking NetSuite about it: it stays a human's
    problem instead of being silently declared 'never landed, safe to retry'.
    """
    from app.services.chat.write_side_effect_repo import reconcile_by_external_id

    session, msg = await _seed_pending_write(
        db, {"recordType": "customer", "data": json.dumps({"companyName": "Drill Unstamped Co"})}
    )

    await _approve(
        db,
        session,
        msg,
        json.dumps({"error": "exceeded 60-second timeout limit", INDETERMINATE_KEY: True}),
    )

    pending = await unsettled_for_tenant(db, tenant_id=_TENANT_ID)
    assert len(pending) == 1, "the attempt is still recorded"

    async def _never_called(q: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("must not ask NetSuite about a key it never received")

    from app.services.chat.write_side_effect import SideEffectStatus

    got = await reconcile_by_external_id(db, tenant_id=_TENANT_ID, row=pending[0], suiteql=_never_called)
    assert got is SideEffectStatus.ATTEMPTED
    assert await unsettled_for_tenant(db, tenant_id=_TENANT_ID), "stays for a human"


async def test_a_side_effect_log_failure_does_not_poison_the_session(db):
    """T2 gate round 1, majors x5 — and the fix's own second-order bug.

    The log is best-effort: its comment promises a failure "must not block a
    write a human already approved". Doing it on the caller's session broke
    that twice over. Without a rollback, the poisoned session made the next
    statement raise PendingRollbackError. WITH a rollback, every ORM object in
    the session was expired, so the next `session.id` access lazy-loaded
    outside the greenlet and raised MissingGreenlet — the fix for the poisoned
    session poisoned it differently. Only running the failure path revealed
    that (agent-graph.md #12).

    An isolated session removes the shared fate instead of managing it. What
    this proves is the property both earlier versions violated: after the log
    fails, the caller's session is STILL USABLE and the turn completes.
    """
    session, msg = await _seed_pending_write(
        db, {"recordType": "customer", "data": json.dumps({"companyName": "Drill Poison Co"})}
    )
    before = (await db.execute(text("SELECT count(*) FROM chat_messages"))).scalar()

    boom = AsyncMock(side_effect=RuntimeError("simulated DB failure inside record_attempt"))
    with patch("app.services.chat.write_side_effect_repo.record_attempt", boom):
        await _approve(db, session, msg, json.dumps({"success": True, "recordId": "5264999"}))

    assert boom.await_count == 1, "the failure path must actually have been taken"

    # Would raise PendingRollbackError or MissingGreenlet if the session were
    # poisoned by either earlier implementation.
    after = (await db.execute(text("SELECT count(*) FROM chat_messages"))).scalar()
    assert after > before, "the turn completed and persisted its assistant message"

    # The approved write still went through — degraded to pre-log behaviour.
    assert msg.structured_output["status"] == "approved"
    # ...and nothing was logged, since logging is what failed.
    assert await unsettled_for_tenant(db, tenant_id=_TENANT_ID) == []
