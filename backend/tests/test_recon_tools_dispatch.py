"""Dispatch-boundary regression tests for the recon MCP tool family.

T2 gate finding (R3-B follow-up): the ONLY production caller of these tools is
chat → ``app/services/chat/tools.py`` → ``mcp_server.call_tool`` →
``governed_execute``, which invokes ``execute_fn(validated_params,
context=context)`` — db/tenant_id/actor_id all arrive inside a single
``context=`` kwarg. The recon family used to read bare ``kwargs.get("db")`` /
``kwargs.get("tenant_id")``, so through the REAL dispatch every call returned
``{"success": False, "error": "Missing database session or tenant context"}``
and the chat discovery/approve surface was unreachable end-to-end. All other
tool tests call ``execute()`` directly with bare kwargs, which is exactly why
this breakage was invisible to the suite.

These tests go through ``mcp_server.call_tool`` — the same registry-level
dispatch shape chat uses — one per recon tool, so the context-convention
contract can never silently regress again. They run WITHOUT a database
(stub sessions), like ``test_recon_exceptions_tool.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.mcp.server import mcp_server
from app.models.audit import AuditEvent
from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
)

# ---------------------------------------------------------------------------
# Stub sessions — governed_execute audits via db.add()/db.flush(), and each
# tool issues its own statements via db.execute(). The stubs accept BOTH so
# governance's audit writes succeed without a real database.
# ---------------------------------------------------------------------------


class _SelectStubResult:
    """Window-count pair shape served by recon.get_exceptions' single stmt."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return [(r, len(self._rows)) for r in self._rows]


class _ExceptionsStubDB:
    """Serves recon.get_exceptions; records statements for inspection."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.stmts = []
        self.added = []

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return _SelectStubResult(self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _ScalarStubResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ApproveStubDB:
    """Serves recon.approve_match's two sequential lookups (result, then run)."""

    def __init__(self, recon_result, run=None):
        self._scalar_values = [recon_result, run]
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        return _ScalarStubResult(self._scalar_values.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


class _RunStubDB:
    """recon.run only passes the session through to ReconJobRunner."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


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


def _compiled(stmt) -> tuple[str, dict]:
    compiled = stmt.compile(compile_kwargs={"render_postcompile": True})
    return str(compiled), compiled.params


# ---------------------------------------------------------------------------
# recon.get_exceptions — bucket="rules" must be reachable through dispatch
# ---------------------------------------------------------------------------


async def test_get_exceptions_receives_context_through_governed_dispatch():
    """The R3-B headline claim — chat's discovery surface for suggested fuzzy
    matches (bucket="rules") — must hold through the REAL dispatch, not just
    direct ``execute(db=..., tenant_id=...)`` test calls."""
    row = _make_result(status="suggested", match_type="fuzzy", bucket=BUCKET_RULES)
    db = _ExceptionsStubDB([row])
    tenant_id = uuid.uuid4()

    out = await mcp_server.call_tool(
        tool_name="recon.get_exceptions",
        params={"run_id": str(uuid.uuid4()), "bucket": "rules"},
        tenant_id=str(tenant_id),
        actor_id=str(uuid.uuid4()),
        db=db,
    )

    # NOT the missing-context error — db/tenant_id arrived via context=.
    assert out["success"] is True, out
    assert out["bucket"] == BUCKET_RULES
    assert out["exception_count"] == 1
    assert out["exceptions"][0]["status"] == "suggested"
    # The query the tool issued is tenant-scoped to the CALLER's tenant.
    select_stmts = db.stmts
    assert len(select_stmts) == 1
    sql, params = _compiled(select_stmts[0])
    assert "reconciliation_results.tenant_id = " in sql
    assert str(tenant_id) in params.values()


# ---------------------------------------------------------------------------
# recon.approve_match — the chat approve workflow's write half
# ---------------------------------------------------------------------------


async def test_approve_match_receives_context_through_governed_dispatch():
    """fetch bucket="rules" → recon_approve_match is only "reachable again" if
    the approve half ALSO works through dispatch — including mapping context's
    ``actor_id`` to the approving user so ``approved_by`` + the per-line audit
    actor are stamped (HITL invariant), never silently NULL."""
    row = _make_result(status="suggested", match_type="fuzzy", bucket=BUCKET_RULES)
    db = _ApproveStubDB(row, run=None)
    actor = uuid.uuid4()

    out = await mcp_server.call_tool(
        tool_name="recon.approve_match",
        params={"result_id": str(row.id)},
        tenant_id=str(row.tenant_id),
        actor_id=str(actor),
        db=db,
    )

    assert out["success"] is True, out
    assert out["status"] == "approved"
    assert row.status == "approved"
    # actor_id from the governed context stamps approved_by...
    assert row.approved_by == actor
    assert row.approved_at is not None
    assert db.committed is True
    # ...and the per-line recon.approve audit row carries the same actor.
    approve_events = [e for e in db.added if isinstance(e, AuditEvent) and e.action == "recon.approve"]
    assert len(approve_events) == 1
    assert approve_events[0].actor_id == actor
    assert approve_events[0].resource_id == str(row.id)


# ---------------------------------------------------------------------------
# recon.run — trigger surface
# ---------------------------------------------------------------------------


async def test_recon_run_receives_context_through_governed_dispatch(monkeypatch):
    import app.mcp.tools.recon_run as mod

    captured = {}

    class _FakeRunner:
        def __init__(self, db, tenant_id):
            captured["db"] = db
            captured["tenant_id"] = tenant_id

        async def run(self, date_from, date_to, payout_ids=None):
            return SimpleNamespace(
                run_id=str(uuid.uuid4()),
                status="completed",
                total_payouts=1,
                total_deposits=1,
                matched_count=1,
                exception_count=0,
                unmatched_count=0,
                total_variance=Decimal("0"),
                match_rate=Decimal("100.0"),
            )

    monkeypatch.setattr(mod, "ReconJobRunner", _FakeRunner)
    db = _RunStubDB()
    tenant_id = uuid.uuid4()

    out = await mcp_server.call_tool(
        tool_name="recon.run",
        params={"date_from": "2026-01-01", "date_to": "2026-01-31"},
        tenant_id=str(tenant_id),
        actor_id=str(uuid.uuid4()),
        db=db,
    )

    assert out["success"] is True, out
    # The session + tenant the runner received came through context=.
    assert captured["db"] is db
    assert captured["tenant_id"] == str(tenant_id)


# ---------------------------------------------------------------------------
# recon.get_evidence — no db dependency, but the dispatch shape must work
# ---------------------------------------------------------------------------


async def test_get_evidence_works_through_governed_dispatch():
    run_id = str(uuid.uuid4())

    out = await mcp_server.call_tool(
        tool_name="recon.get_evidence",
        params={"run_id": run_id},
        tenant_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        db=None,
    )

    assert out["success"] is True, out
    assert out["download_url"] == f"/api/v1/reconciliation/evidence/{run_id}"


# ---------------------------------------------------------------------------
# Direct-kwargs convention stays accepted (every existing tool test uses it)
# ---------------------------------------------------------------------------


async def test_get_exceptions_still_accepts_bare_kwargs():
    db = _ExceptionsStubDB([])

    out = await mcp_server.tools["recon.get_exceptions"]["execute"](
        {"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4()
    )

    assert out["success"] is True
    assert out["bucket"] == BUCKET_NEEDS_REVIEW


async def test_missing_context_still_returns_structured_error_through_dispatch():
    """db=None through the real dispatch → the structured missing-context
    error (not a crash) — governance audits are skipped when db is None."""
    out = await mcp_server.call_tool(
        tool_name="recon.get_exceptions",
        params={"run_id": str(uuid.uuid4())},
        tenant_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        db=None,
    )

    assert out["success"] is False
    assert "Missing database session or tenant context" in out["error"]
