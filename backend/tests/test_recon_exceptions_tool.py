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

from app.mcp.tools.recon_exceptions import execute
from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import BUCKET_NEEDS_REVIEW

# ---------------------------------------------------------------------------
# Stub session — records the statement, serves in-memory rows
# ---------------------------------------------------------------------------


class _StubScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _StubDB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        return _StubScalarResult(self.rows)


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
    # Ordering + cap preserved.
    assert "ORDER BY reconciliation_results.variance_amount DESC" in sql
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
    assert "abs(" not in sql


async def test_min_variance_invalid_returns_error():
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4()), "min_variance": "not-a-number"}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is False
    assert "min_variance" in out["error"]


# ---------------------------------------------------------------------------
# Existing guard behavior preserved
# ---------------------------------------------------------------------------


async def test_missing_run_id_returns_error():
    out = await execute({}, db=_StubDB([]), tenant_id=uuid.uuid4())
    assert out["success"] is False


async def test_missing_db_or_tenant_returns_error():
    out = await execute({"run_id": str(uuid.uuid4())}, db=None, tenant_id=None)
    assert out["success"] is False
