"""Unit tests for the chat MCP tool ``recon.get_exceptions`` (Task A, advisory-coherence).

These run WITHOUT a database: rows are in-memory ``ReconciliationResult``
instances served through a stub session, so they cover the payload re-frame
(``advisory_match_score`` + authoritative ``status``/``bucket``, stripped
``confidence_signals`` WITHOUT mutating the ORM evidence dict) and — via
compiled-SQL inspection — the selection re-key (bucket == needs_review AND
status NOT IN approved/locked) plus the Decimal-safe ``min_variance`` filter.

Row-level selection semantics against real Postgres live in
``test_recon_exceptions_tool_db.py`` (PM runs post-flight).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.mcp.tools.recon_exceptions import execute
from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import BUCKET_NEEDS_REVIEW

# ---------------------------------------------------------------------------
# Stub session — records the statement, serves in-memory rows
# ---------------------------------------------------------------------------


class _StubResult:
    """Serves BOTH statement shapes the tool issues: the count query
    (``scalar_one``) and the row select (``scalars().all()``)."""

    def __init__(self, rows, count):
        self._rows = rows
        self._count = count

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._count


class _StubDB:
    def __init__(self, rows=None, count=None):
        self.rows = rows or []
        # TRUE filtered total the count query reports — defaults to len(rows)
        # but can diverge to simulate the >50-row truncation case.
        self.count = count if count is not None else len(self.rows)
        self.stmts = []

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return _StubResult(self.rows, self.count)

    @property
    def last_stmt(self):
        return self.stmts[-1] if self.stmts else None


def _compiled(stmt) -> tuple[str, dict]:
    """Compile to (sql, params) — UUID columns can't render as literals,
    so assert structure via the SQL text and values via the bind params."""
    compiled = stmt.compile(compile_kwargs={"render_postcompile": True})
    return str(compiled), compiled.params


def _make_result(**overrides) -> ReconciliationResult:
    defaults = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        match_type="unmatched",
        confidence=Decimal("0"),
        status="pending",
        bucket=BUCKET_NEEDS_REVIEW,
        stripe_amount=Decimal("100.00"),
        netsuite_amount=None,
        variance_amount=Decimal("100.00"),
        variance_type="missing",
        variance_explanation=None,
        currency="USD",
        match_rule=None,
        evidence=None,
    )
    defaults.update(overrides)
    return ReconciliationResult(**defaults)


# ---------------------------------------------------------------------------
# Payload re-frame
# ---------------------------------------------------------------------------


async def test_payload_uses_advisory_match_score_and_authoritative_disposition():
    row = _make_result(status="auto_matched", match_type="deterministic", confidence=Decimal("0.4200"))
    db = _StubDB([row])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert out["exception_count"] == 1
    exc = out["exceptions"][0]
    # Advisory composite renamed — never framed as a verdict.
    assert exc["advisory_match_score"] == "0.4200"
    assert "confidence" not in exc
    # Authoritative disposition fields present per row.
    assert exc["status"] == "auto_matched"
    assert exc["bucket"] == BUCKET_NEEDS_REVIEW


async def test_confidence_signals_stripped_without_mutating_orm_evidence():
    stored_evidence = {
        "order_reference": "R123456789",
        "confidence_signals": {"amount_score": "0.9", "composite": "0.91"},
    }
    row = _make_result(evidence=stored_evidence)
    db = _StubDB([row])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    exc = out["exceptions"][0]
    # Calibration instrumentation stripped from the LLM-facing payload...
    assert "confidence_signals" not in exc["evidence"]
    assert exc["evidence"]["order_reference"] == "R123456789"
    # ...but the ORM row's evidence dict was NOT mutated (a filtered copy was built).
    assert "confidence_signals" in row.evidence
    assert row.evidence is stored_evidence
    assert exc["evidence"] is not row.evidence


async def test_none_evidence_passes_through_without_crash():
    row = _make_result(evidence=None)
    db = _StubDB([row])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert out["exceptions"][0]["evidence"] is None


# ---------------------------------------------------------------------------
# Selection re-key (compiled-SQL inspection; row-level proof in the _db twin)
# ---------------------------------------------------------------------------


async def test_selection_is_bucket_keyed_not_status_keyed():
    db = _StubDB([])

    await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    sql, params = _compiled(db.last_stmt)
    # Authoritative selection: the needs_review bucket...
    assert "reconciliation_results.bucket = " in sql
    assert "needs_review" in params.values()
    # ...excluding already-dispositioned rows.
    assert "reconciliation_results.status NOT IN " in sql
    assert "approved" in params.values()
    assert "locked" in params.values()
    # The old status-keyed selection must be gone (suggested/pending are not the key).
    assert "reconciliation_results.status IN " not in sql
    assert "suggested" not in params.values()
    assert "pending" not in params.values()
    # Ordering (largest ABSOLUTE variance first — signed desc would bury
    # negative-variance refund-heavy rows below the cap) + cap preserved.
    assert "ORDER BY abs(reconciliation_results.variance_amount) DESC" in sql
    assert "ORDER BY reconciliation_results.variance_amount DESC" not in sql
    assert 50 in params.values()


async def test_query_is_tenant_scoped():
    """The stmt must carry the tenant_id predicate bound to the CALLER's tenant.

    The run_id filter alone does NOT prove tenant scoping — a cross-tenant row
    can share a run_id (``ReconciliationResult.tenant_id`` is a plain column),
    and the conftest ``db`` fixture connects as table owner so RLS does not
    backstop the in-query filter in tests. This regression class is live in
    this repo: commit 34b8f50 fixed exactly a missing tenant filter in the
    evidence-download query. Row-level proof: the _db twin seeds a
    cross-tenant row on the SAME run and asserts it is excluded.
    """
    db = _StubDB([])
    tenant_id = uuid.uuid4()

    await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=tenant_id)

    sql, params = _compiled(db.last_stmt)
    assert "reconciliation_results.tenant_id = " in sql
    assert str(tenant_id) in params.values()


# ---------------------------------------------------------------------------
# min_variance — documented param, now implemented (Decimal-safe abs filter)
# ---------------------------------------------------------------------------


async def test_min_variance_applies_decimal_safe_abs_filter():
    db = _StubDB([])

    await execute({"run_id": str(uuid.uuid4()), "min_variance": "50.00"}, db=db, tenant_id=uuid.uuid4())

    sql, params = _compiled(db.last_stmt)
    assert "abs(reconciliation_results.variance_amount) >= " in sql
    # Decimal-safe: bound as Decimal, never float.
    assert Decimal("50.00") in params.values()


async def test_min_variance_absent_adds_no_filter():
    db = _StubDB([])

    await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    sql, _params = _compiled(db.last_stmt)
    # No abs FILTER in the WHERE clause (the abs ORDER BY is always present).
    assert "abs(reconciliation_results.variance_amount) >= " not in sql


async def test_min_variance_invalid_returns_error():
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4()), "min_variance": "not-a-number"}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is False
    assert "min_variance" in out["error"]


@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-Infinity", "inf", "sNaN"])
async def test_min_variance_non_finite_returns_error(value):
    """NaN/Infinity/sNaN parse as valid Decimals but silently match ZERO rows
    at the SQL layer — the tool would report 'no exceptions' for a run full
    of them. They must be rejected, not swallowed."""
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4()), "min_variance": value}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is False
    assert "min_variance" in out["error"]
    # Rejected up front — no query was ever issued.
    assert db.stmts == []


# ---------------------------------------------------------------------------
# Count honesty — TRUE total vs returned vs truncated (no-LLM-numbers)
# ---------------------------------------------------------------------------


async def test_exception_count_is_true_total_with_returned_and_truncated():
    """``exception_count`` must be the server-side total over the filters,
    NOT len() after the 50-row cap — the no-LLM-numbers framing promises the
    authoritative bucket size."""
    rows = [_make_result() for _ in range(50)]  # what the capped select returns
    db = _StubDB(rows, count=120)  # the TRUE filtered total

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert out["exception_count"] == 120
    assert out["returned"] == 50
    assert out["truncated"] is True


async def test_not_truncated_when_total_fits_in_cap():
    rows = [_make_result(), _make_result()]
    db = _StubDB(rows)  # count defaults to len(rows) == 2

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["exception_count"] == 2
    assert out["returned"] == 2
    assert out["truncated"] is False


async def test_count_query_shares_filters_with_select_and_has_no_cap():
    """The count stmt runs over the SAME filters as the row select (incl. the
    optional min_variance abs filter) but without the 50-row cap."""
    db = _StubDB([])

    await execute({"run_id": str(uuid.uuid4()), "min_variance": "50.00"}, db=db, tenant_id=uuid.uuid4())

    assert len(db.stmts) == 2  # count first, then the capped row select
    count_sql, count_params = _compiled(db.stmts[0])
    select_sql, _ = _compiled(db.stmts[1])

    assert "count(" in count_sql
    assert "ORDER BY" in select_sql  # sanity: stmts[1] is the row select
    # Same filters on the count: tenant + run + bucket + open-status + min_variance.
    assert "reconciliation_results.tenant_id = " in count_sql
    assert "reconciliation_results.run_id = " in count_sql
    assert "reconciliation_results.bucket = " in count_sql
    assert "needs_review" in count_params.values()
    assert "reconciliation_results.status NOT IN " in count_sql
    assert "abs(reconciliation_results.variance_amount) >= " in count_sql
    assert Decimal("50.00") in count_params.values()
    # The TRUE total is uncapped (no LIMIT clause; a params check would be
    # ambiguous here — Decimal("50.00") == 50, the min_variance bind).
    assert "LIMIT" not in count_sql


def test_registry_description_mentions_row_cap_and_true_total():
    """The LLM frames the result from the registry description — it must know
    about the 50-row largest-|variance|-first cap and that exception_count is
    the TRUE total (so a truncated list is never presented as exhaustive)."""
    from app.mcp.registry import TOOL_REGISTRY

    desc = TOOL_REGISTRY["recon.get_exceptions"]["description"]
    assert "50" in desc
    assert "absolute variance" in desc.lower()
    assert "exception_count" in desc
    assert "truncated" in desc


# ---------------------------------------------------------------------------
# Zero-amount serialization — Decimal("0.00") is falsy but NOT null
# ---------------------------------------------------------------------------


async def test_zero_amounts_serialize_as_zero_not_null():
    row = _make_result(stripe_amount=Decimal("0.00"), netsuite_amount=Decimal("0.00"))
    db = _StubDB([row])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    exc = out["exceptions"][0]
    assert exc["stripe_amount"] == "0.00"
    assert exc["netsuite_amount"] == "0.00"


async def test_none_amounts_still_serialize_as_null():
    row = _make_result(stripe_amount=None, netsuite_amount=None)
    db = _StubDB([row])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    exc = out["exceptions"][0]
    assert exc["stripe_amount"] is None
    assert exc["netsuite_amount"] is None


# ---------------------------------------------------------------------------
# _evidence_for_llm helper — module-level, guards the no-signals case
# ---------------------------------------------------------------------------


def test_evidence_for_llm_helper_guards_no_signals_case():
    from app.mcp.tools.recon_exceptions import _evidence_for_llm

    assert _evidence_for_llm(None) is None
    # Common case: no signals → same object back, no pointless copy.
    plain = {"order_reference": "R123456789"}
    assert _evidence_for_llm(plain) is plain
    # Signals present → filtered COPY; the source dict is never mutated.
    with_signals = {"order_reference": "R1", "confidence_signals": {"composite": "0.9"}}
    out = _evidence_for_llm(with_signals)
    assert out == {"order_reference": "R1"}
    assert "confidence_signals" in with_signals


# ---------------------------------------------------------------------------
# Canonical bucket predicate reuse
# ---------------------------------------------------------------------------


async def test_bucket_predicate_reuses_canonical_bucket_conditions(monkeypatch):
    """The selection must go through the canonical SQL twin
    ``bucket_conditions(BUCKET_NEEDS_REVIEW)`` (consistency with every other
    bucket-keyed query), not a hand-inlined ``bucket == ...``."""
    import app.mcp.tools.recon_exceptions as mod

    calls = []
    real = mod.bucket_conditions  # AttributeError here == predicate not reused

    def _spy(bucket):
        calls.append(bucket)
        return real(bucket)

    monkeypatch.setattr(mod, "bucket_conditions", _spy)

    db = _StubDB([])
    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert calls and all(c == BUCKET_NEEDS_REVIEW for c in calls)
    # Behavior parity: the compiled predicate is unchanged.
    sql, params = _compiled(db.last_stmt)
    assert "reconciliation_results.bucket = " in sql
    assert "needs_review" in params.values()


# ---------------------------------------------------------------------------
# Existing guard behavior preserved
# ---------------------------------------------------------------------------


async def test_missing_run_id_returns_error():
    out = await execute({}, db=_StubDB([]), tenant_id=uuid.uuid4())
    assert out["success"] is False


async def test_missing_db_or_tenant_returns_error():
    out = await execute({"run_id": str(uuid.uuid4())}, db=None, tenant_id=None)
    assert out["success"] is False
