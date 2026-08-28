# backend/tests/test_celigo_error_signatures.py
"""Task 6: `app/services/celigo/errors.py` -- error snapshotting + signature
grouping. See that module's docstring for the design; this file proves it.

Fixtures use SYNTHETIC values only (no real Celigo payloads, no real
customer emails/order refs) -- a human ruled this session that same-SHAPE
invented values are the only acceptable fixture data here, because the real
customer emails and order refs the task brief names verbatim would put
customer PII into git history permanently (this file does not repeat them,
even to explain why -- that would defeat the point). `R000000001`-style
refs and `user@example.test`/`deleted_user_000000@user.deleted`-style
emails mirror the real shapes without being real.

STEP 5 NOTE: this file does NOT assert any specific total signature count
(the brief's "~3 signatures" figure is 17 days stale per
observed-shapes.md -- the live account had 103 open errors across 13+ flows
on 2026-08-27, not 30 across 4). Every test here asserts a PROPERTY of the
normalizer instead: same-shape variants collapse, a structurally different
error does not, and no PII survives into the fingerprint.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlowError, CeligoFlowStep
from app.services.celigo.errors import fingerprint, normalize_message, upsert_errors
from app.services.celigo.repository import (
    extract_flow_steps,
    sync_flow_steps,
    upsert_flow,
    upsert_integration,
)
from app.services.celigo.sanitizer import sanitize
from tests.conftest import create_test_tenant

# ---------------------------------------------------------------------------
# Synthetic corpus -- same SHAPE as the real (PII-bearing) corpus named in
# the task brief, values invented. See module docstring.
# ---------------------------------------------------------------------------

_SHIP_ADDRESS_VARIANTS = [
    (
        "R000000001",
        "deleted_user_000001@user.deleted",
    ),
    (
        "R000000002",
        "deleted_user_000002@user.deleted",
    ),
    (
        "R000000003",
        "deleted_user_000003@user.deleted",
    ),
    (
        "R000000004",
        "deleted_user_000004@user.deleted",
    ),
]


def _ship_address_message(order_ref: str, email: str) -> str:
    return f"MISSING_SHIP_ADDRESS: order {order_ref} has no ship address on file for customer {email}"


def _value_lookup_message(email: str) -> str:
    return f"value_lookup_failed: no matching customer record found for lookup email {email}"


_TYPE_ERROR_MESSAGE = "TypeError: Cannot read properties of null (reading 'name')"


def _raw_error(
    *,
    celigo_id: str,
    source: str | None = "import",
    code: str | None = "MISSING_SHIP_ADDRESS",
    message: str,
    occurred_at: str | None = "2026-08-20T10:00:00Z",
) -> dict:
    """One error shaped like `sanitizer.py`'s `_ERROR` allowlist (see that
    module's field list) -- this is what `client.list_flow_errors_for_step`
    hands a caller, and what `upsert_errors`' `raw_errors` param expects."""
    return {
        "errorId": celigo_id,
        "traceKey": f"trace_{celigo_id}",
        "retryDataKey": f"retry_{celigo_id}",
        "source": source,
        "code": code,
        "message": message,
        "occurredAt": occurred_at,
        "purgeAt": "2026-09-19T10:00:00Z",
        "_flowJobId": "job_1",
        "retriable": True,
    }


async def _make_connection(db: AsyncSession, tenant_id) -> uuid.UUID:
    """Raw SQL, not the `Connection` ORM model -- `celigo_write_guard.py`
    refuses any ORM flush of a provider='celigo' row outside the paired
    connect/disconnect endpoints. Mirrors `test_celigo_repository.py`'s
    identical helper."""
    conn_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant_id, 'celigo', 'Celigo', 'active', 'unit-test-not-a-real-token', 1)"
        ).bindparams(id=conn_id, tenant_id=tenant_id)
    )
    await db.flush()
    return conn_id


async def _seed_step(db: AsyncSession, tenant_id, connection_id, *, suffix: str) -> CeligoFlowStep:
    """Seed one integration + flow + flow step via the repository's own
    upsert functions (Task 5), then read back the real ORM row -- `step` is
    what an orchestrator naturally has in hand after syncing a flow's steps:
    something exposing `.id` (celigo_flow_steps PK) and `.flow_id`
    (celigo_flows PK), which `upsert_errors` needs to attach errors to the
    right rows."""
    integration_id = await upsert_integration(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        sanitized=sanitize("integration", {"_id": f"int_{suffix}", "name": "Test Integration"}),
    )
    flow_id = await upsert_flow(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        integration_id=integration_id,
        sanitized=sanitize(
            "flow",
            {
                "_id": f"flow_{suffix}",
                "name": "Test Flow",
                "_integrationId": f"int_{suffix}",
                "pageGenerators": [{"_exportId": f"exp_{suffix}"}],
            },
        ),
    )
    flow = sanitize("flow", {"_id": f"flow_{suffix}", "pageGenerators": [{"_exportId": f"exp_{suffix}"}]})
    steps = extract_flow_steps(flow)
    step_ids = await sync_flow_steps(db, tenant_id=tenant_id, connection_id=connection_id, flow_id=flow_id, steps=steps)
    await db.flush()
    step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.id == step_ids[0]))).scalar_one()
    return step


# ---------------------------------------------------------------------------
# fingerprint() / normalize_message() -- pure, no DB.
# ---------------------------------------------------------------------------


class TestNormalizeMessageStripsPii:
    def test_order_ref_and_email_do_not_survive_normalization(self):
        msg = _ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])
        normalized = normalize_message(msg)
        assert "@" not in normalized
        assert re.search(r"\bR\d+\b", normalized) is None

    def test_lookup_email_does_not_survive_normalization(self):
        msg = _value_lookup_message("user@example.test")
        normalized = normalize_message(msg)
        assert "@" not in normalized

    def test_fingerprint_hex_digest_contains_no_email_or_order_ref(self):
        """Belt-and-suspenders: even though a hex digest can never literally
        contain '@', prove the INPUT to the hash (not just its output) was
        scrubbed -- see the two tests above, which are the meaningful half
        of this guarantee. This test pins the public fingerprint() entry
        point to the same property."""
        msg = _ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])
        fp = fingerprint("import", "MISSING_SHIP_ADDRESS", msg)
        assert "@" not in fp
        assert re.search(r"\bR\d+\b", fp) is None


class TestFingerprintCollapsesSameShapeVariants:
    def test_four_order_ref_and_email_variants_share_one_fingerprint(self):
        fps = {
            fingerprint("import", "MISSING_SHIP_ADDRESS", _ship_address_message(ref, email))
            for ref, email in _SHIP_ADDRESS_VARIANTS
        }
        assert len(fps) == 1

    def test_two_email_only_variants_share_one_fingerprint(self):
        fp1 = fingerprint("import", "value_lookup_failed", _value_lookup_message("user_alpha@example.test"))
        fp2 = fingerprint("import", "value_lookup_failed", _value_lookup_message("user_beta@example.test"))
        assert fp1 == fp2

    def test_structurally_different_error_gets_its_own_fingerprint(self):
        ship_fp = fingerprint("import", "MISSING_SHIP_ADDRESS", _ship_address_message(*_SHIP_ADDRESS_VARIANTS[0]))
        type_error_fp = fingerprint(None, None, _TYPE_ERROR_MESSAGE)
        assert ship_fp != type_error_fp

    def test_source_and_code_participate_in_the_fingerprint(self):
        """Two identical messages under a different source/code are NOT the
        same failure -- the fingerprint must not collapse on message text
        alone."""
        msg = "generic timeout"
        fp_a = fingerprint("import", "TIMEOUT", msg)
        fp_b = fingerprint("export", "TIMEOUT", msg)
        assert fp_a != fp_b


# ---------------------------------------------------------------------------
# upsert_errors() -- DB-backed.
# ---------------------------------------------------------------------------


class TestUpsertErrorsGroupsBySignature:
    async def test_four_variant_messages_collapse_to_one_signature_occurrence_count_4(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="ship")

        raw_errors = [
            _raw_error(celigo_id=f"err_ship_{i}", message=_ship_address_message(ref, email))
            for i, (ref, email) in enumerate(_SHIP_ADDRESS_VARIANTS)
        ]

        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=raw_errors)
        await db.flush()

        sig_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert sig_count == 1

        row = (
            await db.execute(
                text(
                    "SELECT occurrence_count, sample_message FROM celigo_error_signatures WHERE tenant_id = :t"
                ).bindparams(t=tenant.id)
            )
        ).one()
        assert row.occurrence_count == 4
        # sample_message is PII-bearing by design (spec-preserved verbatim);
        # this test only proves it was stored, never asserts a raw value in
        # a log or error string. See sanitizer.py's own posture on `message`.
        assert row.sample_message is not None

        error_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert error_count == 4

    async def test_two_email_variants_collapse_to_one_signature(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="lookup")

        raw_errors = [
            _raw_error(
                celigo_id="err_lookup_1",
                code="value_lookup_failed",
                message=_value_lookup_message("user_alpha@example.test"),
            ),
            _raw_error(
                celigo_id="err_lookup_2",
                code="value_lookup_failed",
                message=_value_lookup_message("user_beta@example.test"),
            ),
        ]

        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=raw_errors)
        await db.flush()

        sig_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert sig_count == 1

    async def test_structurally_different_error_gets_its_own_signature(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="mixed")

        raw_errors = [
            _raw_error(celigo_id="err_a", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
            _raw_error(celigo_id="err_b", source=None, code=None, message=_TYPE_ERROR_MESSAGE),
        ]

        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=raw_errors)
        await db.flush()

        sig_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert sig_count == 2

    async def test_no_stored_signature_row_contains_an_email_or_order_ref(self, db: AsyncSession):
        """Fingerprint AND sample_message/message columns are checked
        separately: `message`/`sample_message` are PII-bearing BY DESIGN
        (never scrubbed -- 'the message IS the diagnosis'); only the
        `fingerprint` column itself must be PII-free. This test pins that
        distinction at the DB level, not just in the pure fingerprint()
        unit tests above."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="pii")

        raw_errors = [
            _raw_error(celigo_id=f"err_pii_{i}", message=_ship_address_message(ref, email))
            for i, (ref, email) in enumerate(_SHIP_ADDRESS_VARIANTS)
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=raw_errors)
        await db.flush()

        fingerprints = (
            (
                await db.execute(
                    text("SELECT fingerprint FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(t=tenant.id)
                )
            )
            .scalars()
            .all()
        )
        for fp in fingerprints:
            assert "@" not in fp
            assert re.search(r"\bR\d+\b", fp) is None


class TestOccurrenceCountReflectsResolutionWithinSameCall:
    """FIX ROUND 1 (team lead, 2026-08-27, caught by an executed repro, not
    a reading): `mark_flow_errors_resolved` used to run AFTER the phase-3
    occurrence_count recompute, so a signature's count stayed stale by
    exactly the set THIS call just resolved -- and if that root cause never
    reappears in any future batch, nothing ever corrects it. This test
    fails against that ordering (occurrence_count_after would read 2, not
    1) and passes now that resolution runs before the recompute."""

    async def test_occurrence_count_drops_when_an_error_resolves_in_the_same_call(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="occ")

        first_sync = [
            _raw_error(celigo_id="err_occ_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
            _raw_error(celigo_id="err_occ_vanishes", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[1])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=first_sync)
        await db.flush()

        occurrence_count_before = (
            await db.execute(
                text("SELECT occurrence_count FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(
                    t=tenant.id
                )
            )
        ).scalar_one()
        assert occurrence_count_before == 2  # both errors open, same signature

        # Resync: err_occ_vanishes is gone -- same signature, one fewer open
        # error. This is the call that must both resolve AND recompute
        # correctly, since nothing later is guaranteed to touch this
        # signature again.
        second_sync = [
            _raw_error(celigo_id="err_occ_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=second_sync)
        await db.flush()

        occurrence_count_after = (
            await db.execute(
                text("SELECT occurrence_count FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(
                    t=tenant.id
                )
            )
        ).scalar_one()
        assert occurrence_count_after == 1  # NOT 2 -- must reflect THIS call's own resolution

        vanished = (
            await db.execute(
                select(CeligoFlowError).where(
                    CeligoFlowError.tenant_id == tenant.id, CeligoFlowError.celigo_id == "err_occ_vanishes"
                )
            )
        ).scalar_one()
        assert vanished.resolved_at is not None  # row survives, resolved -- never deleted


class TestOccurrenceCountRecomputesOrphanedSignatures:
    """FIX ROUND 2 (team lead, 2026-08-27, proven with a probe against
    shipped code, not a reading): round 1's ordering fix only covers the
    TRANSIENT case, where a resolving error's signature has a surviving
    sibling somewhere in the same call -- that sibling is what makes
    `signature_ids` (built exclusively from *raw_errors*) include the
    signature at all. This is the PERMANENT case round 1 could not catch:
    one signature, one error, and a resync with a COMPLETELY EMPTY
    *raw_errors* for that step -- no sibling anywhere, on this step or any
    other step touched in the call. Before this fix, nothing in phase 3
    would ever recompute that signature again; `occurrence_count` would
    freeze at 1 forever even though the underlying error is resolved."""

    async def test_occurrence_count_drops_to_zero_with_no_sibling_anywhere_in_the_call(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="orphan")

        first_sync = [
            _raw_error(celigo_id="err_orphan_only", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=first_sync)
        await db.flush()

        occurrence_count_before = (
            await db.execute(
                text("SELECT occurrence_count FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(
                    t=tenant.id
                )
            )
        ).scalar_one()
        assert occurrence_count_before == 1

        # Resync with an EMPTY raw_errors list -- the error is simply gone,
        # with NO sibling anywhere in this call to keep its signature
        # "represented". This is the whole point of the test: it must not
        # accidentally recreate the transient (sibling-present) case.
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=[])
        await db.flush()

        occurrence_count_after = (
            await db.execute(
                text("SELECT occurrence_count FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(
                    t=tenant.id
                )
            )
        ).scalar_one()
        assert occurrence_count_after == 0

        orphaned = (
            await db.execute(
                select(CeligoFlowError).where(
                    CeligoFlowError.tenant_id == tenant.id, CeligoFlowError.celigo_id == "err_orphan_only"
                )
            )
        ).scalar_one()
        assert orphaned.resolved_at is not None  # never deleted -- row survives, marked resolved


class TestUpsertErrorsIsIdempotent:
    async def test_same_batch_synced_twice_does_not_double_the_count(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="repeat")

        raw_errors = [
            _raw_error(celigo_id=f"err_repeat_{i}", message=_ship_address_message(ref, email))
            for i, (ref, email) in enumerate(_SHIP_ADDRESS_VARIANTS)
        ]

        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=raw_errors)
        await db.flush()
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=raw_errors)
        await db.flush()

        error_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert error_count == 4  # not 8

        occurrence_count = (
            await db.execute(
                text("SELECT occurrence_count FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(
                    t=tenant.id
                )
            )
        ).scalar_one()
        assert occurrence_count == 4  # not 8


class TestStep5RealisticLongTailedCorpus:
    """Step 5 of the brief ("prove the grouping on real numbers") -- but NOT
    tuned to the brief's stale "30 errors / 4 flows / ~3 signatures" figure
    (observed-shapes.md: that's 17 days old and off by 3x+; the live account
    had 103 open errors across 13+ flows on 2026-08-27, long-tailed, not
    evenly spread -- two flows alone carry 32 and 24).

    This corpus is SHAPED like that reality (a long tail: one big flow, one
    medium flow, several small ones) but built from KNOWN root causes, so
    the expected signature count isn't a guess -- it's exactly the number of
    distinct templates used, which this test controls and asserts exactly.
    A synthetic corpus can prove grouping fidelity in a way a live count
    never can, because only here is the ground truth known.
    """

    async def test_five_distinct_root_causes_across_five_steps_collapse_to_five_signatures(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        def ship_address_errors(n: int, id_prefix: str) -> list[dict]:
            return [
                _raw_error(
                    celigo_id=f"{id_prefix}_{i}",
                    source="import",
                    code="MISSING_SHIP_ADDRESS",
                    message=_ship_address_message(f"R{i:09d}", f"deleted_user_{i:06d}@user.deleted"),
                )
                for i in range(n)
            ]

        def timeout_errors(n: int, id_prefix: str) -> list[dict]:
            return [
                _raw_error(
                    celigo_id=f"{id_prefix}_{i}",
                    source="export",
                    code="TIMEOUT",
                    message=f"Request timed out after {1000 + i * 37}ms",
                )
                for i in range(n)
            ]

        def lookup_errors(n: int, id_prefix: str) -> list[dict]:
            return [
                _raw_error(
                    celigo_id=f"{id_prefix}_{i}",
                    source="import",
                    code="value_lookup_failed",
                    message=_value_lookup_message(f"user{i}@example.test"),
                )
                for i in range(n)
            ]

        def type_errors(n: int, id_prefix: str) -> list[dict]:
            return [
                _raw_error(celigo_id=f"{id_prefix}_{i}", source=None, code=None, message=_TYPE_ERROR_MESSAGE)
                for i in range(n)
            ]

        def duplicate_errors(n: int, id_prefix: str) -> list[dict]:
            return [
                _raw_error(
                    celigo_id=f"{id_prefix}_{i}",
                    source="import",
                    code="DUPLICATE_RECORD",
                    message=f"Duplicate order detected for order R{900000000 + i}",
                )
                for i in range(n)
            ]

        # Flow 1 -- the "32-error" flow: mostly one root cause.
        step_1 = await _seed_step(db, tenant.id, conn_id, suffix="big1")
        await upsert_errors(
            db, tenant_id=tenant.id, connection_id=conn_id, step=step_1, raw_errors=ship_address_errors(20, "big1")
        )

        # Flow 2 -- the "24-error" flow: a different root cause entirely.
        step_2 = await _seed_step(db, tenant.id, conn_id, suffix="big2")
        await upsert_errors(
            db, tenant_id=tenant.id, connection_id=conn_id, step=step_2, raw_errors=timeout_errors(12, "big2")
        )

        # Three small flows -- the long tail. One of them (step_5) ALSO
        # emits a few MISSING_SHIP_ADDRESS variants, proving the same
        # signature collapses correctly ACROSS flows, not just within one.
        step_3 = await _seed_step(db, tenant.id, conn_id, suffix="small3")
        await upsert_errors(
            db, tenant_id=tenant.id, connection_id=conn_id, step=step_3, raw_errors=lookup_errors(3, "small3")
        )
        step_4 = await _seed_step(db, tenant.id, conn_id, suffix="small4")
        await upsert_errors(
            db, tenant_id=tenant.id, connection_id=conn_id, step=step_4, raw_errors=type_errors(1, "small4")
        )
        step_5 = await _seed_step(db, tenant.id, conn_id, suffix="small5")
        cross_flow_ship_address = [
            _raw_error(
                celigo_id=f"small5_shipaddr_{i}",
                source="import",
                code="MISSING_SHIP_ADDRESS",
                message=_ship_address_message(f"R{500000000 + i}", f"deleted_user_{500000 + i}@user.deleted"),
            )
            for i in range(2)
        ]
        await upsert_errors(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            step=step_5,
            raw_errors=duplicate_errors(4, "small5") + cross_flow_ship_address,
        )
        await db.flush()

        total_errors = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert total_errors == 20 + 12 + 3 + 1 + 4 + 2  # == 42

        signatures = (
            await db.execute(
                text(
                    "SELECT fingerprint, occurrence_count FROM celigo_error_signatures WHERE tenant_id = :t"
                ).bindparams(t=tenant.id)
            )
        ).all()
        # Exactly 5 distinct root causes were used -- this is the number
        # this test file actually observed and records for task-6-report.md,
        # NOT the brief's stale "~3". Grouping collapsed 42 raw errors to 5
        # signatures: real evidence the normalizer neither over- nor
        # under-merges on THIS known-ground-truth corpus.
        assert len(signatures) == 5

        by_count = sorted((row.occurrence_count for row in signatures), reverse=True)
        # The MISSING_SHIP_ADDRESS signature spans step_1 (20) + step_5 (2)
        # -- proof the grouping is cross-flow, not per-step.
        assert by_count == [22, 12, 4, 3, 1]


class TestErrorsAreNeverDeletedOnlyResolved:
    """The non-negotiable this task exists to protect: an error present in a
    previous sync but absent from the current one is NOT deleted -- it is
    marked resolved and its row survives."""

    async def test_error_missing_from_new_sync_is_resolved_not_deleted(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="preserve")

        first_sync = [
            _raw_error(celigo_id="err_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
            _raw_error(celigo_id="err_vanishes", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[1])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=first_sync)
        await db.flush()

        # Second sync: err_vanishes is gone (Celigo no longer reports it).
        second_sync = [
            _raw_error(celigo_id="err_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=second_sync)
        await db.flush()

        # Row count unchanged -- NEVER deleted.
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 2

        vanished = (
            await db.execute(
                select(CeligoFlowError).where(
                    CeligoFlowError.tenant_id == tenant.id, CeligoFlowError.celigo_id == "err_vanishes"
                )
            )
        ).scalar_one()
        assert vanished.resolved_at is not None
        assert vanished.purged_at is None  # this module never marks purged -- see its docstring

        stays = (
            await db.execute(
                select(CeligoFlowError).where(
                    CeligoFlowError.tenant_id == tenant.id, CeligoFlowError.celigo_id == "err_stays"
                )
            )
        ).scalar_one()
        assert stays.resolved_at is None

    def test_errors_module_exposes_no_delete_function(self):
        """Same cheap durable guard test_celigo_repository.py uses for
        `celigo_flow_errors` -- pinned here too since this is the module
        that actually decides what happens to a vanished error."""
        import app.services.celigo.errors as errors_module

        delete_like = [name for name in dir(errors_module) if "delete" in name.lower()]
        assert delete_like == []


class TestIncompleteRawErrorsNeverResolvesAnything:
    """WHOLE-BRANCH REVIEW FINDING 4 (2026-08-27) -- defense-in-depth, second
    layer: `client.list_flow_errors_for_step` now raises rather than truncate
    (see that module's own fix), so today's only caller can never hand this
    function a partial listing. But the invariant "resolving an absence is
    only correct against a COMPLETE listing" used to live only in the
    fetcher -- one layer away from the resolution logic that depends on it.
    `raw_errors_is_complete=False` is that same invariant enforced a second
    time, here, so a FUTURE fetcher that reintroduces truncation cannot
    silently reopen this bug without also affirmatively (and wrongly)
    claiming completeness."""

    async def test_false_never_resolves_an_error_absent_from_a_partial_listing(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="partial")

        first_sync = [
            _raw_error(celigo_id="err_partial_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
            _raw_error(celigo_id="err_partial_missing", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[1])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=first_sync)
        await db.flush()

        # A hypothetical truncated re-fetch: only ONE of the two previously-
        # open errors came back this time, but the caller KNOWS (and says
        # so) that this is not the complete listing.
        partial_resync = [
            _raw_error(celigo_id="err_partial_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
        ]
        await upsert_errors(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            step=step,
            raw_errors=partial_resync,
            raw_errors_is_complete=False,
        )
        await db.flush()

        missing = (
            await db.execute(
                select(CeligoFlowError).where(
                    CeligoFlowError.tenant_id == tenant.id, CeligoFlowError.celigo_id == "err_partial_missing"
                )
            )
        ).scalar_one()
        # The whole point: absence from a KNOWN-partial listing must never
        # be read as "this error is gone now".
        assert missing.resolved_at is None

    async def test_true_default_still_resolves_against_a_complete_listing(self, db: AsyncSession):
        """Regression guard: the new parameter must not change today's
        default (complete-listing) behavior, already pinned by
        TestErrorsAreNeverDeletedOnlyResolved above -- restated here so this
        test class stands alone as proof of both directions."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        step = await _seed_step(db, tenant.id, conn_id, suffix="complete")

        first_sync = [
            _raw_error(celigo_id="err_complete_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
            _raw_error(celigo_id="err_complete_vanishes", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[1])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=first_sync)
        await db.flush()

        second_sync = [
            _raw_error(celigo_id="err_complete_stays", message=_ship_address_message(*_SHIP_ADDRESS_VARIANTS[0])),
        ]
        await upsert_errors(db, tenant_id=tenant.id, connection_id=conn_id, step=step, raw_errors=second_sync)
        await db.flush()

        vanished = (
            await db.execute(
                select(CeligoFlowError).where(
                    CeligoFlowError.tenant_id == tenant.id, CeligoFlowError.celigo_id == "err_complete_vanishes"
                )
            )
        ).scalar_one()
        assert vanished.resolved_at is not None
