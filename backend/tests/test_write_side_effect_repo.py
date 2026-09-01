"""Recording and reconciling side effects — the durable half.

`write_side_effect.py` derives the key and classifies an answer. This is the
part that must survive the process dying: the row goes in BEFORE the call, and
resume settles it from EVIDENCE rather than by retrying blind.

These tests run against a real Postgres (the drill DB), because the properties
that matter here are database properties — a server-side default that applies
when the caller never gets to set it, and a unique constraint that makes a
second attempt update one row instead of appending a second.
"""

import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.chat.write_side_effect import SideEffectStatus
from app.services.chat.write_side_effect_repo import (
    record_attempt,
    settle_from_result,
    unsettled_for_tenant,
)

DRILL_URL = os.environ.get("DRILL_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/idem_drill")
pytestmark = pytest.mark.asyncio

_TENANT = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(DRILL_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(text("DELETE FROM write_side_effects"))
        # The tenant FK is real; make sure the row exists in the drill DB.
        await s.execute(
            text(
                "INSERT INTO tenants (id, name, slug, created_at, updated_at) "
                "VALUES (:i,'Drill','drill',now(),now()) ON CONFLICT (id) DO NOTHING"
            ),
            {"i": _TENANT},
        )
        await s.commit()
        yield s
    await engine.dispose()


async def test_attempt_is_durable_before_any_answer(db):
    """The row must exist, as 'attempted', with nothing else set — this is what
    a crash between send and confirm leaves behind."""
    key = await record_attempt(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-abc",
        record_type="customer",
        mutation_type="create",
        payload={"companyName": "Acme"},
        correlation_id="c1",
    )
    await db.commit()
    row = (
        await db.execute(
            text("SELECT status, netsuite_record_id FROM write_side_effects WHERE idempotency_key=:k"),
            {"k": "ss-idem-abc"},
        )
    ).first()
    assert row.status == SideEffectStatus.ATTEMPTED.value
    assert row.netsuite_record_id is None
    assert key == "ss-idem-abc"


async def test_status_default_is_enforced_by_the_database(db):
    """Not just by the model. The row is inserted precisely because the process
    may die immediately after, so the safe state cannot depend on Python."""
    await db.execute(
        text(
            "INSERT INTO write_side_effects (id, tenant_id, idempotency_key, record_type, mutation_type) "
            "VALUES (:i,:t,'ss-idem-raw','customer','create')"
        ),
        {"i": uuid.uuid4(), "t": _TENANT},
    )
    await db.commit()
    got = (await db.execute(text("SELECT status FROM write_side_effects WHERE idempotency_key='ss-idem-raw'"))).scalar()
    assert got == "attempted"


async def test_a_second_attempt_updates_one_row_not_two(db):
    """Two attempts at the same work share a row. Appending would make the log
    imply two writes where there was one — a log that lies about side effects
    is worse than no log."""
    for _ in range(2):
        await record_attempt(
            db,
            tenant_id=_TENANT,
            idempotency_key="ss-idem-dup",
            record_type="customer",
            mutation_type="create",
            payload={"companyName": "Acme"},
            correlation_id="c1",
        )
        await db.commit()
    n = (await db.execute(text("SELECT count(*) FROM write_side_effects WHERE idempotency_key='ss-idem-dup'"))).scalar()
    assert n == 1


async def test_success_settles_to_written_with_the_record_id(db):
    await record_attempt(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-ok",
        record_type="customer",
        mutation_type="create",
        payload={},
        correlation_id="c1",
    )
    await db.commit()
    await settle_from_result(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-ok",
        raw_result=json.dumps({"success": True, "recordId": "5264548"}),
    )
    await db.commit()
    row = (
        await db.execute(
            text("SELECT status, netsuite_record_id FROM write_side_effects WHERE idempotency_key='ss-idem-ok'")
        )
    ).first()
    assert row.status == "written"
    assert row.netsuite_record_id == "5264548"


async def test_a_timeout_leaves_it_attempted(db):
    """The whole point. An indeterminate answer must not collapse into either
    outcome — it stays the state that says 'go and look'."""
    from app.services.chat.write_outcome import INDETERMINATE_KEY

    await record_attempt(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-to",
        record_type="customer",
        mutation_type="create",
        payload={},
        correlation_id="c1",
    )
    await db.commit()
    await settle_from_result(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-to",
        raw_result=json.dumps({"error": "exceeded 60-second timeout limit", INDETERMINATE_KEY: True}),
    )
    await db.commit()
    got = (await db.execute(text("SELECT status FROM write_side_effects WHERE idempotency_key='ss-idem-to'"))).scalar()
    assert got == "attempted"


async def test_duplicate_refusal_settles_to_written(db):
    """NetSuite refusing our externalId proves the original landed. Measured
    live: HTTP 400 'This entity already exists'."""
    await record_attempt(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-exists",
        record_type="customer",
        mutation_type="create",
        payload={},
        correlation_id="c1",
    )
    await db.commit()
    await settle_from_result(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-exists",
        raw_result=json.dumps(
            {
                "success": False,
                "error": '{"o:errorDetails":[{"detail":"Error while accessing a resource. '
                'This entity already exists.","o:errorCode":"USER_ERROR"}]}',
            }
        ),
    )
    await db.commit()
    got = (
        await db.execute(text("SELECT status FROM write_side_effects WHERE idempotency_key='ss-idem-exists'"))
    ).scalar()
    assert got == "written"


async def test_unsettled_lists_exactly_what_needs_resolving(db):
    """The resume query. 'attempted' rows are the ones a crash left behind."""
    for key, result in (
        ("ss-idem-w", json.dumps({"success": True, "recordId": "1"})),
        ("ss-idem-r", json.dumps({"error": "Please enter value(s) for: Subsidiary."})),
        ("ss-idem-a", None),
    ):
        await record_attempt(
            db,
            tenant_id=_TENANT,
            idempotency_key=key,
            record_type="customer",
            mutation_type="create",
            payload={},
            correlation_id="c1",
        )
        await db.commit()
        if result:
            await settle_from_result(db, tenant_id=_TENANT, idempotency_key=key, raw_result=result)
            await db.commit()

    pending = await unsettled_for_tenant(db, tenant_id=_TENANT)
    assert [p.idempotency_key for p in pending] == ["ss-idem-a"]


async def test_reconcile_refuses_when_the_key_was_never_sent(db):
    """A key we invented locally but did NOT put in the payload cannot be found
    in NetSuite — not because the write failed, but because we never sent it.

    Concluding 'rejected, safe to retry' from that empty result is the exact
    proxy-predicate defect this table exists to end: the query tests a stand-in
    (a key NetSuite never saw) for the real condition (did the write land).
    Reached whenever a card is built by a path that does not stamp — so it must
    be structurally impossible to get wrong, not merely avoided by callers.
    """
    from app.services.chat.write_side_effect_repo import reconcile_by_external_id

    await record_attempt(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-unsent",
        record_type="customer",
        mutation_type="create",
        payload={"companyName": "Acme"},  # <- no externalId: the key never left this process
        correlation_id="c1",
    )
    await db.commit()

    row = (await unsettled_for_tenant(db, tenant_id=_TENANT))[0]
    asked = []

    async def fake_suiteql(q: str) -> str:
        asked.append(q)
        return '{"data": []}'

    status = await reconcile_by_external_id(db, tenant_id=_TENANT, row=row, suiteql=fake_suiteql)

    assert status is SideEffectStatus.ATTEMPTED, "an unanswerable question must not settle the row"
    assert asked == [], "must not even ask — an empty answer here would be meaningless"
    assert await unsettled_for_tenant(db, tenant_id=_TENANT), "row stays on the worklist for a human"


async def test_reconcile_still_works_when_the_key_was_sent(db):
    """The stamped case is unaffected: the key IS in the payload, so an empty
    result really does mean the write never landed."""
    from app.services.chat.write_side_effect_repo import reconcile_by_external_id

    await record_attempt(
        db,
        tenant_id=_TENANT,
        idempotency_key="ss-idem-sent",
        record_type="customer",
        mutation_type="create",
        payload={"companyName": "Acme", "externalId": "ss-idem-sent"},
        correlation_id="c1",
    )
    await db.commit()

    row = (await unsettled_for_tenant(db, tenant_id=_TENANT))[0]

    async def fake_suiteql(q: str) -> str:
        return '{"data": []}'

    status = await reconcile_by_external_id(db, tenant_id=_TENANT, row=row, suiteql=fake_suiteql)
    assert status is SideEffectStatus.REJECTED
