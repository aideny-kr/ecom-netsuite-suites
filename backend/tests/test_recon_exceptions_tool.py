"""Unit tests for the chat MCP tool ``recon.get_exceptions`` (Task A + R3-B).

These run WITHOUT a database: rows are in-memory ``ReconciliationResult``
instances served through a stub session, so they cover the payload re-frame
(``advisory_match_score`` + authoritative ``status``/``bucket``, stripped
``confidence_signals`` WITHOUT mutating the ORM evidence dict) and — via
compiled-SQL inspection — the bucket-keyed selection (validated optional
``bucket`` param defaulting to needs_review, AND status NOT IN
approved/locked), the Decimal-safe non-negative ``min_variance`` filter, and
the SINGLE-statement count/select shape (``count(*) OVER ()`` window — count
and rows can never disagree under READ COMMITTED).

Row-level selection semantics against real Postgres live in
``test_recon_exceptions_tool_db.py`` (PM runs post-flight).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.mcp.tools.recon_exceptions import execute
from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import (
    ALL_BUCKETS,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
)

# ---------------------------------------------------------------------------
# Stub session — records the statement, serves in-memory rows
# ---------------------------------------------------------------------------


class _StubResult:
    """Serves the tool's SINGLE statement shape: each result row is a
    ``(ReconciliationResult, total_count)`` pair — the window count
    (``count(*) OVER ()``) is carried on every row, exactly as real Postgres
    returns it. There is deliberately NO ``scalar_one``/``scalars`` here: a
    separate count query (the old two-statement shape) must fail loudly."""

    def __init__(self, rows, count):
        self._rows = rows
        self._count = count

    def all(self):
        return [(r, self._count) for r in self._rows]


class _StubDB:
    def __init__(self, rows=None, count=None):
        self.rows = rows or []
        # TRUE filtered total the window count reports — defaults to len(rows)
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


@pytest.mark.parametrize("value", ["-0.01", "-50", -25])
async def test_min_variance_negative_returns_error(value):
    """The filter is on ABSOLUTE variance, so a negative threshold is
    always-true — a silent no-op that LOOKS like it filtered. Reject it with
    the same structured-error shape as non-finite values."""
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4()), "min_variance": value}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is False
    assert "min_variance" in out["error"]
    # Rejected up front — no query was ever issued.
    assert db.stmts == []


# ---------------------------------------------------------------------------
# Optional bucket param — validated, defaults to needs_review (R3-B #1)
# ---------------------------------------------------------------------------


async def test_bucket_defaults_to_needs_review_and_is_echoed():
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    # The response says which bucket it actually queried (honest framing).
    assert out["bucket"] == BUCKET_NEEDS_REVIEW
    _sql, params = _compiled(db.last_stmt)
    assert "needs_review" in params.values()


async def test_bucket_rules_selects_the_suggested_fuzzy_population():
    """bucket="rules" restores chat's discovery surface for the fuzzy-match
    rules bucket (mostly status=suggested awaiting approval) that the
    needs_review re-key made invisible (T2 gate finding #1). NOT the close
    gate's "suggested" count — that is status-keyed across all buckets."""
    row = _make_result(
        status="suggested",
        match_type="fuzzy",
        bucket=BUCKET_RULES,
        confidence=Decimal("0.8500"),
        variance_type="fx_rounding",
        variance_amount=Decimal("0.30"),
    )
    db = _StubDB([row])

    out = await execute({"run_id": str(uuid.uuid4()), "bucket": "rules"}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert out["bucket"] == BUCKET_RULES
    exc = out["exceptions"][0]
    assert exc["status"] == "suggested"
    assert exc["bucket"] == BUCKET_RULES
    sql, params = _compiled(db.last_stmt)
    assert "reconciliation_results.bucket = " in sql
    assert "rules" in params.values()
    assert "needs_review" not in params.values()
    # The dispositioned-status exclusion applies to EVERY bucket.
    assert "reconciliation_results.status NOT IN " in sql
    assert "approved" in params.values()
    assert "locked" in params.values()


@pytest.mark.parametrize("bucket", list(ALL_BUCKETS))
async def test_bucket_accepts_every_canonical_bucket(bucket):
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4()), "bucket": bucket}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert out["bucket"] == bucket
    _sql, params = _compiled(db.last_stmt)
    assert bucket in params.values()


@pytest.mark.parametrize("bad", ["bogus", "", "NEEDS_REVIEW", "exceptions"])
async def test_bucket_invalid_returns_structured_error(bad):
    """Invalid bucket → structured error (same shape as the other param
    errors), validated against ALL_BUCKETS BEFORE any query is issued."""
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4()), "bucket": bad}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is False
    assert "bucket" in out["error"]
    assert db.stmts == []


# ---------------------------------------------------------------------------
# Count honesty — TRUE total via a SINGLE statement (window count)
# ---------------------------------------------------------------------------


async def test_exception_count_is_true_total_with_returned_and_truncated():
    """``exception_count`` must be the server-side total over the filters,
    NOT len() after the 50-row cap — the framing promises the authoritative
    bucket size."""
    rows = [_make_result() for _ in range(50)]  # what the capped select returns
    db = _StubDB(rows, count=120)  # the TRUE filtered total (window value)

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


async def test_count_and_rows_come_from_one_statement():
    """Count + rows in ONE statement (single snapshot): two separate
    statements can disagree under READ COMMITTED when a concurrent commit
    lands between them. The row select carries ``count(*) OVER ()`` — the
    window is computed over the FULL filtered set BEFORE LIMIT applies."""
    db = _StubDB([_make_result()])

    out = await execute({"run_id": str(uuid.uuid4()), "min_variance": "50.00"}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert len(db.stmts) == 1  # the single snapshot — no separate count query
    sql, params = _compiled(db.last_stmt)
    assert "count(*) OVER ()" in sql
    # The one statement carries ALL the filters + ordering + cap.
    assert "reconciliation_results.tenant_id = " in sql
    assert "reconciliation_results.run_id = " in sql
    assert "reconciliation_results.bucket = " in sql
    assert "reconciliation_results.status NOT IN " in sql
    assert "abs(reconciliation_results.variance_amount) >= " in sql
    assert Decimal("50.00") in params.values()
    assert "ORDER BY abs(reconciliation_results.variance_amount) DESC" in sql
    assert "LIMIT" in sql


async def test_zero_rows_yield_zero_total_without_a_second_query():
    """LIMIT-only (no OFFSET): zero rows returned PROVES the filtered set is
    empty, so the true total is 0 — no fallback count query is needed (and
    issuing one would reintroduce the two-snapshot disagreement window)."""
    db = _StubDB([])

    out = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is True
    assert out["exception_count"] == 0
    assert out["returned"] == 0
    assert out["truncated"] is False
    assert len(db.stmts) == 1


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
# Honest framing — bucket param documented + verbatim-numbers instruction
# (R3-B #1 + #4: NOT an intercepted no-LLM-numbers surface today, so the
# registry description and the knowledge profile carry the mitigation)
# ---------------------------------------------------------------------------


def test_registry_entry_documents_bucket_param_and_verbatim_numbers():
    from app.mcp.registry import TOOL_REGISTRY

    entry = TOOL_REGISTRY["recon.get_exceptions"]
    desc = entry["description"]
    # bucket="rules" is the discovery surface for suggested fuzzy matches
    # awaiting approval (the close gate's "Approve Suggested Matches" population).
    assert 'bucket="rules"' in desc
    assert "suggested" in desc.lower()
    assert "approv" in desc.lower()
    # Verbatim-transcription instruction (raw numbers reach the LLM un-intercepted).
    assert "verbatim" in desc.lower()
    assert "never recompute" in desc.lower()
    assert "exactly" in desc.lower()
    # params_schema: optional bucket param listing the canonical bucket values.
    bucket_schema = entry["params_schema"]["bucket"]
    assert bucket_schema["required"] is False
    for b in ALL_BUCKETS:
        assert b in bucket_schema["description"]


def test_module_docstring_states_interception_reality():
    """The module must NOT claim to be a protected no-LLM-numbers surface:
    recon tools have no tool_categories._EXACT entry, so there is no
    data_table SSE interception — raw amounts flow to the LLM. The docstring
    states reality + the verbatim-transcription mitigation + the logged
    SSE-interception follow-up."""
    import app.mcp.tools.recon_exceptions as mod

    doc = mod.__doc__.lower()
    assert "intercept" in doc
    assert "not" in doc
    assert "verbatim" in doc
    assert "follow-up" in doc


def test_reconciliation_profile_routes_rules_bucket_to_approve_workflow():
    """The chat approve-workflow must be reachable again: the profile tells
    the model to fetch bucket="rules" and approve via recon_approve_match —
    and to transcribe numbers VERBATIM. Tool NAMES stay unchanged
    (capability-sync invariant)."""
    from app.services.chat.knowledge_profiles.loader import load_all_profiles

    profile = next(p for p in load_all_profiles() if p.profile_id == "reconciliation")
    # Collapse the YAML block-scalar line wrapping so phrase assertions are
    # robust to where lines break.
    frag = " ".join(profile.prompt_fragment.split())
    assert 'bucket="rules"' in frag
    assert "recon_approve_match" in frag
    assert "verbatim" in frag.lower()
    assert "exception_count" in frag
    # Advisory framing retained: the score is never a verdict.
    assert "advisory_match_score" in frag
    assert "NEVER present it as a verdict" in frag
    # Tool NAMES unchanged (test_prompt_tool_sync / capability-sync).
    assert set(profile.trigger_tools) == {
        "recon_run",
        "recon_get_exceptions",
        "recon_get_evidence",
        "recon_approve_match",
    }


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


# ---------------------------------------------------------------------------
# Malformed run_id — structured error, never an uncaught ValueError (R4-B #14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-uuid", "12345", "R123456789", 12345])
async def test_malformed_run_id_returns_structured_error(bad):
    """An LLM-supplied malformed run_id must come back as the same structured
    ``{"success": False, "error": ...}`` shape as the bucket/min_variance
    validation — never an uncaught ValueError through the dispatch boundary."""
    db = _StubDB([])

    out = await execute({"run_id": bad}, db=db, tenant_id=uuid.uuid4())

    assert out["success"] is False
    assert "run_id" in out["error"]
    # Rejected up front — no query was ever issued.
    assert db.stmts == []


# ---------------------------------------------------------------------------
# Wording coherence — bucket="rules" is NOT the close gate's suggested count
# (R4-B #1: the gate's "suggested" is STATUS-keyed across ALL buckets;
# bucket="rules" is BUCKET-keyed — mostly suggested but also pending — and
# material-variance suggested rows live in the DEFAULT needs_review listing.
# Default-config counterexample: a deterministic 0.90 match with a $10
# material variance is status=suggested, bucket=auto_classifications.)
# ---------------------------------------------------------------------------


def test_registry_description_does_not_equate_rules_with_gate_suggested_count():
    from app.mcp.registry import TOOL_REGISTRY

    desc = TOOL_REGISTRY["recon.get_exceptions"]["description"]
    # The false equivalence is gone...
    assert "population the close gate counts" not in desc
    # ...replaced by the accurate axes: the gate count is STATUS-keyed...
    assert "status-keyed" in desc.lower()
    # ...while bucket="rules" is mostly suggested but ALSO pending...
    assert "pending" in desc.lower()
    # ...so neither listing equals the gate count — list BOTH to investigate.
    assert "neither" in desc.lower()
    assert "both" in desc.lower()
    assert "needs_review" in desc


def test_tool_docstring_does_not_equate_rules_with_gate_suggested_count():
    doc = execute.__doc__
    assert 'the close gate\'s "Approve Suggested Matches" population' not in doc
    assert "status-keyed" in doc.lower()
    assert "pending" in doc.lower()
    assert "neither" in doc.lower()
    assert "both" in doc.lower()


def test_reconciliation_profile_states_gate_count_is_status_keyed():
    from app.services.chat.knowledge_profiles.loader import load_all_profiles

    profile = next(p for p in load_all_profiles() if p.profile_id == "reconciliation")
    frag = " ".join(profile.prompt_fragment.split())
    # Accurate axes stated for the model...
    assert "STATUS-keyed" in frag
    # ...with the both-buckets investigation recommendation...
    assert "BOTH" in frag
    assert 'bucket="rules"' in frag
    # ...and an explicit never-equate instruction.
    assert "never claim" in frag.lower()
