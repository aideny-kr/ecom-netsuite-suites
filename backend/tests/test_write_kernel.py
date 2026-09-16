"""G3.1b: the write kernel's loop, exercised with a fake adapter against the real ledger.

The kernel owns claim → preflight → send (once) → verify and every ledger write; the
adapter owns the provider. These tests pin the contract between the two: which adapter
result lands in which ledger state, that nothing is ever sent twice, and that an adapter
cannot smuggle a receipt without the permit.
"""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops import write_kernel
from app.services.transaction_ops.write_kernel import ExecutionStoppedError, PreconditionChangedError
from tests import test_transaction_ops_dispatch as dispatch_fixtures

ready = dispatch_fixtures.ready
FINGERPRINT = "d" * 64


@dataclass
class FakeAdapter:
    """A provider in a box: what preflight raises, what send returns, what verify proves."""

    name: str = "fake"
    provider: str = "netsuite"
    preflight_error: Exception | None = None
    receipt: dict | None = None
    send_error: Exception | None = None
    reserve: bool = True
    proof: dict | None = None
    verify_error: Exception | None = None
    sends: int = 0
    reads: list = field(default_factory=list)

    async def preflight(self, db, tenant_id, claimed, *, read):
        self.reads.append("preflight")
        if self.preflight_error:
            raise self.preflight_error
        return {"fresh": True}

    async def send(self, db, tenant_id, claimed, preflight):
        self.sends += 1
        if self.reserve:
            assert await state.reserve_operation_dispatch(
                db, tenant_id, claimed, provider=self.provider, payload_fingerprint=FINGERPRINT
            )
        if self.send_error:
            raise self.send_error
        return self.receipt or {"status": "accepted", "record_id": "63", "verified": False}

    async def verify(self, db, tenant_id, claimed, preflight, *, read):
        self.reads.append("verify")
        if self.verify_error:
            raise self.verify_error
        return self.proof


async def _row(db, claim):
    return await db.scalar(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))


async def _run(db, ready, adapter):
    actor, _, _, claim = ready
    result = await write_kernel.execute(db, actor.tenant_id, claim, adapter)
    return result, await _row(db, claim)


async def test_verified_when_the_readback_proves_the_approved_state(db, ready):
    adapter = FakeAdapter(proof={"source_unchanged": True})
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "verified" and result["termination_reason"] == "done"
    assert row.result_json["verification"] == {"source_unchanged": True}
    assert row.result_json["receipt"]["record_id"] == "63"
    assert adapter.sends == 1 and adapter.reads == ["preflight", "verify"]


async def test_changed_evidence_in_preflight_ends_rejected_before_effect_and_never_sends(db, ready):
    adapter = FakeAdapter(preflight_error=PreconditionChangedError("approved_evidence_changed"))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "rejected_before_effect" and result["termination_reason"] == "error"
    assert row.result_json["code"] == "approved_evidence_changed"
    assert row.result_json.get("dispatch_reserved") is not True
    assert adapter.sends == 0


async def test_a_documented_stop_in_preflight_keeps_its_code(db, ready):
    adapter = FakeAdapter(preflight_error=ExecutionStoppedError("source_payment_failed", keep_code=True))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "rejected_before_effect"
    assert row.result_json["code"] == "source_payment_failed"
    assert adapter.sends == 0


async def test_an_undocumented_stop_never_becomes_ledger_evidence(db, ready):
    adapter = FakeAdapter(preflight_error=ExecutionStoppedError("private token in a message"))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "rejected_before_effect"
    assert row.result_json["code"] == "evidence_revalidation_failed"
    assert "private token" not in str(row.result_json)


async def test_an_undocumented_preflight_failure_is_rejected_before_effect_with_the_generic_code(db, ready):
    adapter = FakeAdapter(preflight_error=RuntimeError("private token in a message"))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "rejected_before_effect"
    assert row.result_json["code"] == "evidence_revalidation_failed"
    assert "private token" not in str(row.result_json)


async def test_a_provider_rejection_is_rejected_before_effect_with_the_permit_spent(db, ready):
    adapter = FakeAdapter(receipt={"status": "failed", "code": "guard_rejected", "verified": False})
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "rejected_before_effect"
    assert row.result_json["code"] == "provider_rejected_without_save"
    assert row.result_json["dispatch_reserved"] is True


async def test_an_accepted_receipt_without_proof_is_committed_unverified(db, ready):
    adapter = FakeAdapter(proof=None)
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "committed_unverified" and result["termination_reason"] == "stall"
    assert row.result_json["code"] == "verification_unproven"
    assert row.result_json["receipt"]["record_id"] == "63"


async def test_an_unknown_receipt_without_proof_is_unknown(db, ready):
    adapter = FakeAdapter(receipt={"status": "unknown", "verified": False}, proof=None)
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "unknown"
    assert row.result_json["code"] == "verification_unproven" and "receipt" not in row.result_json


async def test_an_unknown_receipt_can_still_be_verified_by_the_readback(db, ready):
    adapter = FakeAdapter(receipt={"status": "unknown", "verified": False}, proof={"source_unchanged": True})
    result, _ = await _run(db, ready, adapter)
    assert result["status"] == "verified"


async def test_a_crash_after_the_permit_is_unknown_and_leaks_nothing(db, ready):
    adapter = FakeAdapter(send_error=RuntimeError("private billing address token"))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "unknown"
    assert row.result_json["code"] == "verification_unavailable"
    assert "private billing" not in str(row.result_json)


async def test_a_readback_failure_after_a_receipt_stays_committed_unverified(db, ready):
    adapter = FakeAdapter(verify_error=RuntimeError("read timed out"))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "committed_unverified"
    assert row.result_json["code"] == "verification_unavailable"


async def test_a_documented_stop_during_the_readback_keeps_its_code_and_the_receipt(db, ready):
    """A stop the adapter documents (the source order's payment failed) is ledger evidence
    whether it is found before the send or during the readback after a receipt."""
    adapter = FakeAdapter(verify_error=ExecutionStoppedError("source_payment_failed", keep_code=True))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "committed_unverified"
    assert row.result_json["code"] == "source_payment_failed"
    assert row.result_json["receipt"]["record_id"] == "63"


async def test_changed_evidence_during_the_readback_keeps_the_receipt(db, ready):
    """After a receipt the attempt can never be 'before effect' again, whatever the readback
    finds; the kernel records the finding on the receipted row instead of raising."""
    adapter = FakeAdapter(verify_error=PreconditionChangedError("approved_evidence_changed"))
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "committed_unverified"
    assert row.result_json["code"] == "approved_evidence_changed"
    assert row.result_json["receipt"]["record_id"] == "63"


async def test_a_second_delivery_after_a_closed_receipt_keeps_the_recorded_outcome(db, ready):
    """A committed_unverified attempt the kernel already closed is durable: a re-delivered
    claim (whose send the permit refuses) must not rewrite its recorded code."""
    actor, _, _, claim = ready
    first = await write_kernel.execute(db, actor.tenant_id, claim, FakeAdapter(proof=None))
    assert first["status"] == "committed_unverified"
    second = await write_kernel.execute(db, actor.tenant_id, claim, FakeAdapter(proof=None))
    row = await _row(db, claim)
    assert second["status"] == "committed_unverified"
    assert row.result_json["code"] == "verification_unproven"


async def test_a_redelivery_can_still_verify_a_receipted_attempt(db, ready):
    """A committed_unverified attempt the kernel closed without proof is still open to a
    proof: a later delivery whose send is refused by the permit (the transport answers
    'unknown' without sending) and whose readback proves the state ends verified."""
    actor, _, _, claim = ready
    first = await write_kernel.execute(db, actor.tenant_id, claim, FakeAdapter(proof=None))
    assert first["status"] == "committed_unverified"
    later = FakeAdapter(
        reserve=False, receipt={"status": "unknown", "verified": False}, proof={"source_unchanged": True}
    )
    second = await write_kernel.execute(db, actor.tenant_id, claim, later)
    row = await _row(db, claim)
    assert second["status"] == "verified" and row.status == "verified"
    assert row.result_json["verification"] == {"source_unchanged": True}
    assert row.result_json["receipt"]["record_id"] == "63"  # the first delivery's receipt survives


async def test_a_ledger_read_failure_after_an_exception_leaves_the_attempt_to_expiry_recovery(db, ready, monkeypatch):
    """When the ledger cannot be re-read after a failure the kernel cannot record anything;
    it raises with the adapter failure chained instead of guessing, and the row stays
    executing for recover_expired_operation to settle."""
    from sqlalchemy.exc import OperationalError

    actor, _, _, claim = ready

    async def unavailable(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    adapter = FakeAdapter(preflight_error=RuntimeError("provider read failed"))
    monkeypatch.setattr(write_kernel.state, "_one", unavailable)
    with pytest.raises(OperationalError) as exc:
        await write_kernel.execute(db, actor.tenant_id, claim, adapter)
    assert isinstance(exc.value.__cause__, RuntimeError)
    monkeypatch.undo()
    assert (await _row(db, claim)).status == "executing"


async def test_a_failure_to_record_a_decided_outcome_is_not_reinterpreted(db, ready, monkeypatch):
    """The completion write is not part of the attempt: if it fails, the kernel raises that
    failure and leaves the row as the attempt left it, instead of re-deriving a different
    outcome from the ledger and recording that (a verified attempt must never be written
    down as verification_unavailable because an audit insert hiccuped)."""
    from sqlalchemy.exc import OperationalError

    actor, _, _, claim = ready
    original = state.complete_operation
    calls = []

    async def flaky(*args, **kwargs):
        calls.append(kwargs["outcome"])
        if len(calls) == 1:
            raise OperationalError("INSERT", {}, Exception("connection reset"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(write_kernel.state, "complete_operation", flaky)
    with pytest.raises(OperationalError):
        await write_kernel.execute(db, actor.tenant_id, claim, FakeAdapter(proof={"source_unchanged": True}))
    row = await _row(db, claim)
    assert calls == ["verified"]  # decided once; never re-derived into something else
    assert row.status == "committed_unverified" and "code" not in row.result_json


def test_the_adapter_registry_and_the_ledger_vocabulary_agree():
    """state_service names the provider and adapter the ledger records; write_adapters
    constructs the class. One table must not drift from the other."""
    from app.services.transaction_ops import write_adapters

    for action, provider in state.PROVIDERS.items():
        adapter = write_adapters.build_adapter(action, reads=None, config=None, mapping=None, proposal=None, clock=None)
        assert adapter.provider == provider
        assert adapter.name == state.ADAPTERS[provider]


async def test_an_adapter_cannot_report_a_save_without_the_permit(db, ready):
    """The guard trigger refuses a receipt on a row that never consumed a permit; the kernel
    turns that refusal into needs_review instead of guessing what happened."""
    adapter = FakeAdapter(reserve=False, proof={"source_unchanged": True})
    result, row = await _run(db, ready, adapter)
    assert result["status"] == "needs_review" and result["termination_reason"] == "blocked"
    assert row.result_json["code"] == "adapter_receipt_without_permit"
    assert row.result_json.get("dispatch_reserved") is not True


async def test_a_second_delivery_of_the_same_claim_reads_the_outcome_and_sends_nothing(db, ready):
    actor, _, _, claim = ready
    adapter = FakeAdapter(proof={"source_unchanged": True})
    first = await write_kernel.execute(db, actor.tenant_id, claim, adapter)
    second = await write_kernel.execute(db, actor.tenant_id, claim, FakeAdapter(proof=None))
    assert first["status"] == second["status"] == "verified"
    assert adapter.sends == 1


async def test_a_preflight_that_reserved_a_permit_is_treated_as_possibly_sent(db, ready):
    """Preflight must never reserve. If an adapter does anyway, the kernel refuses to call
    the outcome 'before effect'."""
    actor, _, _, claim = ready

    class Leaky(FakeAdapter):
        async def preflight(self, db, tenant_id, claimed, *, read):
            await state.reserve_operation_dispatch(
                db, tenant_id, claimed, provider="netsuite", payload_fingerprint=FINGERPRINT
            )
            raise PreconditionChangedError("approved_evidence_changed")

    result, row = await _run(db, ready, Leaky())
    assert result["status"] == "unknown"
    assert row.result_json["code"] == "approved_evidence_changed"


async def test_budgeted_reads_are_charged_to_the_operation(db, ready):
    actor, _, _, claim = ready

    class Counting(FakeAdapter):
        async def preflight(self, db, tenant_id, claimed, *, read):
            return await read(3, AsyncMock(return_value={"fresh": True}))

    adapter = Counting(proof={"source_unchanged": True})
    await write_kernel.execute(db, actor.tenant_id, claim, adapter)
    row = await _row(db, claim)
    assert row.api_calls_used == 4  # three budgeted reads plus the one send


async def test_budget_exhaustion_before_the_send_is_rejected_before_effect(db, ready):
    actor, _, _, claim = ready
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=96)

    class Reading(FakeAdapter):
        async def preflight(self, db, tenant_id, claimed, *, read):
            return await read(1, AsyncMock(return_value={}))

    result, row = await _run(db, ready, Reading())
    assert result["status"] == "rejected_before_effect"
    assert row.result_json["code"] == "operation_budget_exhausted"
    assert row.result_json["termination_reason"] == "budget"


@pytest.mark.parametrize("status", ["rejected_before_effect", "verified", "needs_review"])
async def test_the_kernel_never_reopens_a_terminal_row(db, ready, status):
    actor, _, _, claim = ready
    if status != "rejected_before_effect":
        await state.reserve_operation_dispatch(
            db, actor.tenant_id, claim, provider="netsuite", payload_fingerprint=FINGERPRINT
        )
    await state.complete_operation(db, actor.tenant_id, claim.operation_id, outcome=status, result_json={"code": "x"})
    adapter = FakeAdapter(proof={"source_unchanged": True})
    result = await write_kernel.execute(db, actor.tenant_id, claim, adapter)
    assert result["status"] == status
    assert adapter.sends == 1  # the fake sends before the ledger is consulted; the permit refuses
    assert (await _row(db, claim)).status == status


async def test_budget_exhaustion_during_the_readback_keeps_the_budget_reason(db, ready):
    """After a receipt the row is committed_unverified; if the readback runs out of budget the
    ledger already says 'budget' and the kernel must not overwrite it with a generic stall."""
    actor, _, _, claim = ready
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=94)

    class Reading(FakeAdapter):
        async def verify(self, db, tenant_id, claimed, preflight, *, read):
            return await read(5, AsyncMock(return_value={}))  # 94 + 1 send + 5 > 96

    result, row = await _run(db, ready, Reading())
    assert result["status"] == "committed_unverified" and result["termination_reason"] == "budget"
    assert row.result_json["code"] == "operation_budget_exhausted"
    assert row.result_json["receipt"]["record_id"] == "63"
