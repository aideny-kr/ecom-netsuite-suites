# Recon Resolution Phase 1 — Groups Without Money — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the summary-first recon review surface — deterministic ResolutionPlanner, persisted resolution proposals, group-level batch approval, and the redesigned page behind `recon_resolution_ui` — with **zero NetSuite writes** (approve = today's DB flip semantics, grouped).

**Architecture:** A post-run ResolutionPlanner (pure Decimal rule engine, no LLM) maps every non-clean `ReconciliationResult` to a `ReconResolutionProposal` row. Groups are computed by `GROUP BY (root_cause, action, booking_vehicle)`. New endpoints mirror the existing approve-bucket conventions (set-based UPDATE, per-line audit + summary event, correlation_id). The page renders group cards behind the new `recon_resolution_ui` feature flag; flag off = today's UI untouched.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async (`Mapped[]`/`mapped_column`), Alembic, pytest (backend/tests conftest factories), Next.js 14 + React Query + vitest.

**Spec:** `docs/superpowers/specs/2026-07-06-recon-summary-first-resolution-design.md` (Phase 1 of 4).

## Global Constraints

- TDD: failing test first for every task. Backend tests: `backend/.venv/bin/python -m pytest backend/tests/<file> -v` (DB tests need the local docker Postgres harness; run from repo root).
- All money math is `Decimal` — never float. Materiality reuses `tenant_configs.recon_materiality_abs/pct` ($50 / 1% defaults) via `load_materiality()`.
- Migration must run on BOTH DBs: `.venv/bin/alembic upgrade head` (Supabase) AND `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head` (local).
- New feature flag `recon_resolution_ui` defaults OFF (no `tenant_feature_flags` row → `is_enabled` returns False).
- NO NetSuite writes anywhere in this phase. No `posting` transitions occur; the statuses/columns exist for Phase 3.
- Lint before commit: `cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .`; frontend `npx vitest run` for touched components.
- Commit per task; branch `feat/recon-summary-first-resolution`; push to BOTH `origin` and `framework` at the end.
- Advisory confidence stays display-only. Close-lock semantics unchanged except the deliberate `carried_forward` addition (Task 7).

---

### Task 1: Migration `087_recon_resolution_proposals`

**Files:**
- Create: `backend/alembic/versions/087_recon_resolution_proposals.py`
- Test: `backend/tests/test_resolution_proposals_migration.py`

**Interfaces:**
- Produces: table `recon_resolution_proposals` with RLS; partial unique index enforcing ONE active proposal per result (`status IN ('proposed','approved','posting','posted','post_failed')`).

- [ ] **Step 1: Write the failing test**

```python
"""Migration smoke: recon_resolution_proposals exists with RLS + active-unique index."""

from sqlalchemy import text


async def test_recon_resolution_proposals_table_exists(db):
    cols = (
        await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'recon_resolution_proposals'"
            )
        )
    ).scalars().all()
    for expected in (
        "id", "tenant_id", "run_id", "result_id", "root_cause", "action",
        "booking_vehicle", "group_key", "source", "narrative", "evidence",
        "proposed_amount", "currency", "above_materiality", "status",
        "failure_reason", "netsuite_record_refs", "correlation_id",
        "charge_source_id", "decided_by", "decided_at", "created_at", "updated_at",
    ):
        assert expected in cols, f"missing column {expected}"


async def test_rls_forced_on_proposals(db):
    row = (
        await db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'recon_resolution_proposals'"
            )
        )
    ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


async def test_one_active_proposal_per_result_index(db):
    idx = (
        await db.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'recon_resolution_proposals' "
                "AND indexname = 'uq_recon_resolution_proposals_active_result'"
            )
        )
    ).scalar_one()
    assert "UNIQUE" in idx
    assert "proposed" in idx  # partial WHERE clause present
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_proposals_migration.py -v` — expect FAIL (table missing)**

- [ ] **Step 3: Write the migration**

```python
"""recon_resolution_proposals — Phase 1 of the summary-first recon rework.

One row per exception result, written by the ResolutionPlanner (Phase 1) or
ResolutionAgent (Phase 2). Groups are computed, never stored. Partial unique
index = ONE active proposal per result (superseded/rejected rows are history).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "087_recon_resolution_proposals"
down_revision = "086_report_recipe"
branch_labels = None
depends_on = None

ACTIVE_STATUSES = "('proposed','approved','posting','posted','post_failed')"


def upgrade() -> None:
    op.create_table(
        "recon_resolution_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "run_id", UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "result_id", UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_results.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("root_cause", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("booking_vehicle", sa.String(50), nullable=False),
        sa.Column("group_key", sa.String(160), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="planner"),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=True),
        sa.Column("proposed_amount", sa.Numeric(15, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("above_materiality", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("failure_reason", sa.String(50), nullable=True),
        sa.Column("netsuite_record_refs", JSONB(), nullable=True),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        # Denormalized cross-run double-posting guard key (from result.evidence).
        sa.Column("charge_source_id", sa.String(255), nullable=True),
        sa.Column("decided_by", UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_recon_resolution_proposals_tenant", "recon_resolution_proposals", ["tenant_id"])
    op.create_index(
        "ix_recon_resolution_proposals_run_group",
        "recon_resolution_proposals",
        ["run_id", "root_cause", "action", "booking_vehicle"],
    )
    op.create_index("ix_recon_resolution_proposals_corr", "recon_resolution_proposals", ["correlation_id"])
    op.create_index("ix_recon_resolution_proposals_charge", "recon_resolution_proposals", ["tenant_id", "charge_source_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_recon_resolution_proposals_active_result "
        "ON recon_resolution_proposals (result_id) "
        f"WHERE status IN {ACTIVE_STATUSES}"
    )
    op.execute("ALTER TABLE recon_resolution_proposals ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY recon_resolution_proposals_tenant_isolation ON recon_resolution_proposals
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    # load-bearing on Supabase (owner != BYPASSRLS)
    op.execute("ALTER TABLE recon_resolution_proposals FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("recon_resolution_proposals")
```

- [ ] **Step 4: Apply to the LOCAL docker DB and run the test**

Run: `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head`
Then: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_proposals_migration.py -v`
Expected: PASS (3 tests). Do NOT run against Supabase yet — that happens at merge per the deploy flow; note it in the PR body.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/087_recon_resolution_proposals.py backend/tests/test_resolution_proposals_migration.py
git commit -m "feat(recon): recon_resolution_proposals table — one active proposal per result, RLS forced"
```

---

### Task 2: Model + schema literals (`carried_forward`, proposal types)

**Files:**
- Modify: `backend/app/models/reconciliation.py` (append model)
- Modify: `backend/app/schemas/reconciliation.py` (literals + response/request schemas)
- Modify: `backend/app/services/reconciliation/four_bucket_classifier.py` (public `is_material`, extend `TERMINAL_RESULT_STATUSES`)
- Test: `backend/tests/test_resolution_schemas.py`

**Interfaces:**
- Produces: `ReconResolutionProposal` ORM model; `ACTIVE_PROPOSAL_STATUSES` tuple; schemas `ResolutionProposalResponse`, `ResolutionGroupSummary`, `ResolutionSummaryResponse`, `ResolutionGroupApprove`, `ResolutionGroupApproveResult`, `ResolutionGroupRejectResult`, `ResolutionProposalOverride`; literals `ResolutionAction`, `ProposalStatus`, `PostFailureReason`; `ResultStatus` gains `"carried_forward"`; `four_bucket_classifier.is_material(...)` public; `TERMINAL_RESULT_STATUSES` includes `"carried_forward"`.

- [ ] **Step 1: Write the failing test**

```python
"""Schema/model contracts for resolution proposals."""

from decimal import Decimal
from typing import get_args

from app.models.reconciliation import ACTIVE_PROPOSAL_STATUSES, ReconResolutionProposal
from app.schemas.reconciliation import (
    PostFailureReason,
    ProposalStatus,
    ResolutionAction,
    ResolutionProposalResponse,
    ResultStatus,
)
from app.services.reconciliation.four_bucket_classifier import (
    TERMINAL_RESULT_STATUSES,
    is_material,
)


def test_resolution_action_values():
    assert set(get_args(ResolutionAction)) == {
        "book_fee_line", "create_and_apply_deposit", "apply_deposit",
        "credit_memo_refund", "void_duplicate", "writeoff_je",
        "carry_forward", "needs_human",
    }


def test_proposal_status_values():
    assert set(get_args(ProposalStatus)) == {
        "proposed", "approved", "posting", "posted", "rejected", "post_failed", "superseded",
    }
    assert set(ACTIVE_PROPOSAL_STATUSES) == {"proposed", "approved", "posting", "posted", "post_failed"}


def test_failure_reason_values():
    assert set(get_args(PostFailureReason)) == {
        "period_locked", "period_closed", "connection",
        "netsuite_validation", "netsuite_error", "guard_tripped",
    }


def test_result_status_gains_carried_forward():
    assert "carried_forward" in get_args(ResultStatus)
    assert "carried_forward" in TERMINAL_RESULT_STATUSES  # bulk-approve must skip it


def test_is_material_public_helper():
    # $50 abs / 1% pct defaults: $60 variance on a $10k order is material (abs);
    # $40 on a $100 order is material (pct: >$1); $0.40 on a $100 order is not.
    assert is_material(Decimal("60"), Decimal("10000"), Decimal("50"), Decimal("0.01")) is True
    assert is_material(Decimal("40"), Decimal("100"), Decimal("50"), Decimal("0.01")) is True
    assert is_material(Decimal("0.40"), Decimal("100"), Decimal("50"), Decimal("0.01")) is False


def test_proposal_response_from_orm_shape():
    fields = ResolutionProposalResponse.model_fields
    for f in ("id", "run_id", "result_id", "root_cause", "action", "booking_vehicle",
              "group_key", "source", "narrative", "proposed_amount", "currency",
              "above_materiality", "status", "failure_reason", "correlation_id"):
        assert f in fields
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_schemas.py -v` — expect FAIL (imports missing)**

- [ ] **Step 3: Implement**

Append to `backend/app/models/reconciliation.py` (mirror existing `Mapped[]` style; add `Boolean` to the existing sqlalchemy import line):

```python
# Proposal statuses that occupy the one-active-per-result slot (partial unique
# index in migration 087). superseded/rejected rows are retained history.
ACTIVE_PROPOSAL_STATUSES = ("proposed", "approved", "posting", "posted", "post_failed")


class ReconResolutionProposal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "recon_resolution_proposals"
    __table_args__ = (
        Index(
            "ix_recon_resolution_proposals_run_group",
            "run_id", "root_cause", "action", "booking_vehicle",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_results.id", ondelete="CASCADE"), nullable=False
    )
    root_cause: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    booking_vehicle: Mapped[str] = mapped_column(String(50), nullable=False)
    group_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="planner", server_default="planner", nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposed_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    above_materiality: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed", server_default="proposed", nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    netsuite_record_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Cross-run double-posting guard key, denormalized from result.evidence.
    charge_source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Append to `backend/app/schemas/reconciliation.py` (near the other literals, keep field ordering conventions; extend `ResultStatus`):

```python
ResultStatus = Literal[
    "pending", "auto_matched", "suggested", "approved", "rejected",
    "investigating", "locked", "carried_forward",
]

ResolutionAction = Literal[
    "book_fee_line", "create_and_apply_deposit", "apply_deposit",
    "credit_memo_refund", "void_duplicate", "writeoff_je",
    "carry_forward", "needs_human",
]
ProposalStatus = Literal[
    "proposed", "approved", "posting", "posted", "rejected", "post_failed", "superseded",
]
PostFailureReason = Literal[
    "period_locked", "period_closed", "connection",
    "netsuite_validation", "netsuite_error", "guard_tripped",
]


class ResolutionProposalResponse(BaseModel):
    id: StrFromUUID
    run_id: StrFromUUID
    result_id: StrFromUUID
    root_cause: str
    action: str
    booking_vehicle: str
    group_key: str
    source: str
    narrative: str
    proposed_amount: Decimal
    currency: str
    above_materiality: bool
    status: str
    failure_reason: str | None = None
    netsuite_record_refs: dict | None = None
    correlation_id: str | None = None
    decided_by: StrFromUUID | None = None
    decided_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ResolutionGroupSummary(BaseModel):
    group_key: str
    root_cause: str
    action: str
    booking_vehicle: str
    count: int
    proposed_count: int
    approved_count: int
    total_amount: Decimal
    above_materiality_count: int


class ResolutionSummaryResponse(BaseModel):
    """Summary-first payload: one call renders the whole report header + groups.

    ``explained_rate`` = share of proposals whose action is not ``needs_human``
    (diagnostic: a falling rate signals upstream data problems, not just load).
    """

    run_id: str
    total_results: int
    matches_count: int
    match_rate: Decimal
    proposals_count: int
    explained_count: int
    explained_rate: Decimal
    guard_skipped_count: int
    variance_by_root_cause: dict[str, Decimal]
    groups: list[ResolutionGroupSummary]


class ResolutionGroupApprove(BaseModel):
    notes: str | None = None
    # Above-materiality items approve ONLY when explicitly ticked.
    included_above_materiality_ids: list[str] = []
    excluded_ids: list[str] = []


class ResolutionGroupApproveResult(BaseModel):
    run_id: str
    group_key: str
    approved_count: int
    skipped_count: int
    correlation_id: str


class ResolutionGroupRejectResult(BaseModel):
    run_id: str
    group_key: str
    rejected_count: int
    correlation_id: str


class ResolutionProposalOverride(BaseModel):
    action: ResolutionAction
    notes: str | None = None
```

In `backend/app/services/reconciliation/four_bucket_classifier.py`:
1. Locate `TERMINAL_RESULT_STATUSES` (grep it) and append `"carried_forward"` to the tuple, with the comment `# carried_forward: an acknowledged reconciling item — bulk-approve must not flip it.`
2. Add below `_is_material`:

```python
# Public alias — the ResolutionPlanner computes proposal-level materiality with
# the exact same predicate the bucket router uses (single source of truth).
def is_material(
    variance_amount: Decimal | None,
    matched_amount: Decimal | None,
    materiality_abs: Decimal | None,
    materiality_pct: Decimal | None,
) -> bool:
    return _is_material(variance_amount, matched_amount, materiality_abs, materiality_pct)
```

(Check `_is_material`'s actual parameter order first and match it exactly — if it differs from the above, follow the source, and adjust the Task 2 test's argument order to match.)

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_schemas.py -v` — expect PASS. Also run `backend/.venv/bin/python -m pytest backend/tests/test_recon_bucket_reviewer.py -v` (bucket classifier regression).**

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/reconciliation.py backend/app/schemas/reconciliation.py backend/app/services/reconciliation/four_bucket_classifier.py backend/tests/test_resolution_schemas.py
git commit -m "feat(recon): resolution proposal model + schema literals; carried_forward result status"
```

---

### Task 3: ResolutionPlanner rule engine (pure)

**Files:**
- Create: `backend/app/services/reconciliation/resolution_planner.py` (rule engine half; orchestrator added in Task 4)
- Test: `backend/tests/test_resolution_planner.py`

**Interfaces:**
- Produces: `PlannedProposal` frozen dataclass `(root_cause, action, booking_vehicle, group_key, narrative, proposed_amount, above_materiality)`; `plan_result(...) -> PlannedProposal | None` (None = skip); `VEHICLE_BY_ACTION: dict[str, str]`; `group_key_for(root_cause, action, vehicle) -> str`.
- Consumes: `is_material` from Task 2.

- [ ] **Step 1: Write the failing test** (exhaustive over the spec's ordered rule table)

```python
"""ResolutionPlanner rule engine — exhaustive over the spec's ordered rules.

Spec: docs/superpowers/specs/2026-07-06-recon-summary-first-resolution-design.md
(mapping table, rules 1-10; first match wins; evidence rules before variance dispatch).
"""

from decimal import Decimal

from app.services.reconciliation.resolution_planner import (
    VEHICLE_BY_ACTION,
    group_key_for,
    plan_result,
)

MAT = {"materiality_abs": Decimal("50"), "materiality_pct": Decimal("0.01")}


def _plan(**over):
    base = dict(
        match_type="deterministic",
        variance_type=None,
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("100.00"),
        currency="USD",
        variance_explanation=None,
        evidence={"charge_source_id": "ch_1", "order_reference": "R123456789"},
        already_posted=False,
        **MAT,
    )
    base.update(over)
    return plan_result(**base)


def test_rule1_guard_prior_posted_skips():
    assert _plan(already_posted=True, variance_type="fees", variance_amount=Decimal("3.20")) is None


def test_rule2_clean_match_skips():
    assert _plan() is None  # deterministic + zero variance never reaches a proposal


def test_rule3_unapplied_deposit_evidence_wins_over_variance_dispatch():
    p = _plan(
        variance_type="fees", variance_amount=Decimal("3.20"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R123456789", "deposit_unapplied": True},
    )
    assert p.action == "apply_deposit"
    assert p.booking_vehicle == "depositapplication"


def test_rule4_chargeback_policy_gate():
    p = _plan(variance_type="chargeback", variance_amount=Decimal("42.00"))
    assert p.action == "needs_human"
    assert p.booking_vehicle == "none"


def test_rule5_duplicate_voids():
    p = _plan(variance_type="duplicate", variance_amount=Decimal("100.00"))
    assert p.action == "void_duplicate"
    assert p.booking_vehicle == "customerdeposit"
    assert p.proposed_amount == Decimal("100.00")  # netsuite_amount


def test_rule6_fees_book_fee_line():
    p = _plan(variance_type="fees", variance_amount=Decimal("3.20"))
    assert p.action == "book_fee_line"
    assert p.booking_vehicle == "deposit"
    assert p.proposed_amount == Decimal("3.20")
    assert p.root_cause == "fees"
    assert p.group_key == "fees:book_fee_line:deposit"


def test_rule7_missing_with_order_ref_creates_deposit():
    p = _plan(match_type="unmatched", variance_type="missing",
              variance_amount=Decimal("100.00"), netsuite_amount=None)
    assert p.action == "create_and_apply_deposit"
    assert p.booking_vehicle == "customerdeposit"
    assert p.proposed_amount == Decimal("100.00")  # stripe_amount


def test_rule7b_missing_without_order_ref_needs_human():
    p = _plan(match_type="unmatched", variance_type="missing",
              variance_amount=Decimal("100.00"), netsuite_amount=None,
              evidence={"charge_source_id": "ch_1"})
    assert p.action == "needs_human"


def test_rule8_fx_under_materiality_writeoff_flagged_je():
    p = _plan(variance_type="fx_rounding", variance_amount=Decimal("0.04"))
    assert p.action == "writeoff_je"
    assert p.booking_vehicle == "journalentry"
    assert p.above_materiality is False


def test_rule8b_fx_above_materiality_needs_human():
    # $60 > $50 abs threshold on a $10k order
    p = _plan(variance_type="fx_rounding", variance_amount=Decimal("60.00"),
              stripe_amount=Decimal("10000.00"))
    assert p.action == "needs_human"
    assert p.above_materiality is True


def test_rule9_timing_carries_forward():
    p = _plan(variance_type="timing", variance_amount=Decimal("0"))
    assert p.action == "carry_forward"
    assert p.booking_vehicle == "none"
    assert p.proposed_amount == Decimal("0")


def test_rule10_manual_adjustment_needs_human():
    p = _plan(variance_type="manual_adjustment", variance_amount=Decimal("77.10"))
    assert p.action == "needs_human"


def test_rule10b_unknown_variance_type_needs_human_not_crash():
    p = _plan(variance_type="future_type", variance_amount=Decimal("5.00"))
    assert p.action == "needs_human"  # total function: unknown → safe default


def test_above_materiality_set_on_every_proposal():
    p = _plan(variance_type="fees", variance_amount=Decimal("120.00"),
              stripe_amount=Decimal("10000.00"))
    assert p.action == "book_fee_line"  # materiality never changes action selection…
    assert p.above_materiality is True  # …only the bulk-approve eligibility flag


def test_narrative_embeds_explanation_and_no_invented_numbers():
    p = _plan(variance_type="fees", variance_amount=Decimal("3.20"),
              variance_explanation="Variance of $3.20 matches Stripe processing fee")
    assert "Variance of $3.20 matches Stripe processing fee" in p.narrative


def test_group_key_derived_from_columns():
    assert group_key_for("fees", "book_fee_line", "deposit") == "fees:book_fee_line:deposit"
    assert VEHICLE_BY_ACTION["carry_forward"] == "none"
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_planner.py -v` — expect FAIL (module missing)**

- [ ] **Step 3: Implement the rule engine**

```python
"""Deterministic resolution planner — Phase 1 of the summary-first recon rework.

Pure Decimal rule engine: maps one ReconciliationResult's fields to a proposed
resolution (PlannedProposal) or None (skip). NO LLM, NO I/O in plan_result —
the async orchestrator (plan_run, below in this module) owns the DB.

Ordered rules (first match wins; spec mapping table):
  1. already posted in a prior run (guard)      → skip
  2. clean deterministic match, zero variance   → skip (never reaches proposals)
  3. evidence: matched deposit unapplied        → apply_deposit
  4. chargeback / refund-shaped                 → needs_human (policy gate)
  5. duplicate                                  → void_duplicate
  6. fees                                       → book_fee_line
  7. missing + order ref known                  → create_and_apply_deposit
  8. fx_rounding: ≤ materiality → writeoff_je; above → needs_human
  9. timing                                     → carry_forward (no booking, ever)
 10. anything else (manual_adjustment, unknown) → needs_human

Materiality NEVER changes action selection except writeoff_je eligibility
(rule 8); it only sets above_materiality, which gates one-click bulk approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.reconciliation.four_bucket_classifier import is_material

ACTION_BOOK_FEE_LINE = "book_fee_line"
ACTION_CREATE_AND_APPLY = "create_and_apply_deposit"
ACTION_APPLY_DEPOSIT = "apply_deposit"
ACTION_CREDIT_MEMO_REFUND = "credit_memo_refund"
ACTION_VOID_DUPLICATE = "void_duplicate"
ACTION_WRITEOFF_JE = "writeoff_je"
ACTION_CARRY_FORWARD = "carry_forward"
ACTION_NEEDS_HUMAN = "needs_human"

# Canonical booking vehicle per action (multi-write actions use the primary
# record; secondary records land in netsuite_record_refs at posting time).
VEHICLE_BY_ACTION: dict[str, str] = {
    ACTION_BOOK_FEE_LINE: "deposit",
    ACTION_CREATE_AND_APPLY: "customerdeposit",
    ACTION_APPLY_DEPOSIT: "depositapplication",
    ACTION_CREDIT_MEMO_REFUND: "creditmemo",
    ACTION_VOID_DUPLICATE: "customerdeposit",
    ACTION_WRITEOFF_JE: "journalentry",
    ACTION_CARRY_FORWARD: "none",
    ACTION_NEEDS_HUMAN: "none",
}


def group_key_for(root_cause: str, action: str, booking_vehicle: str) -> str:
    return f"{root_cause}:{action}:{booking_vehicle}"


@dataclass(frozen=True)
class PlannedProposal:
    root_cause: str
    action: str
    booking_vehicle: str
    group_key: str
    narrative: str
    proposed_amount: Decimal
    above_materiality: bool


def _mk(
    root_cause: str,
    action: str,
    narrative: str,
    proposed_amount: Decimal,
    above: bool,
) -> PlannedProposal:
    vehicle = VEHICLE_BY_ACTION[action]
    return PlannedProposal(
        root_cause=root_cause,
        action=action,
        booking_vehicle=vehicle,
        group_key=group_key_for(root_cause, action, vehicle),
        narrative=narrative,
        proposed_amount=proposed_amount,
        above_materiality=above,
    )


def plan_result(
    *,
    match_type: str,
    variance_type: str | None,
    variance_amount: Decimal,
    stripe_amount: Decimal | None,
    netsuite_amount: Decimal | None,
    currency: str,
    variance_explanation: str | None,
    evidence: dict | None,
    already_posted: bool,
    materiality_abs: Decimal,
    materiality_pct: Decimal,
) -> PlannedProposal | None:
    """Total, pure. Returns None only for rules 1-2 (skips)."""
    evidence = evidence or {}
    abs_variance = abs(variance_amount)
    above = is_material(variance_amount, stripe_amount, materiality_abs, materiality_pct)
    explain = f" {variance_explanation}" if variance_explanation else ""
    root = variance_type or ("missing" if match_type == "unmatched" else "manual_adjustment")

    # 1. cross-run double-posting guard
    if already_posted:
        return None
    # 2. clean match — nothing to resolve
    if match_type == "deterministic" and variance_type is None and variance_amount == Decimal("0"):
        return None
    # 3. evidence-based rules BEFORE variance-type dispatch
    if evidence.get("deposit_unapplied") is True and netsuite_amount is not None:
        return _mk(
            root, ACTION_APPLY_DEPOSIT,
            f"Deposit exists but is unapplied — apply it to the linked order.{explain}",
            abs_variance, above,
        )
    # 4. policy gate: never auto-propose a booking for a chargeback
    if variance_type == "chargeback":
        return _mk(
            "chargeback", ACTION_NEEDS_HUMAN,
            f"Chargeback/dispute — requires human review before any booking.{explain}",
            abs_variance, above,
        )
    # 5. duplicates: reverse via the same record type (pre-checks at posting time)
    if variance_type == "duplicate":
        return _mk(
            "duplicate", ACTION_VOID_DUPLICATE,
            f"Duplicate deposit — void/reverse the original customer deposit.{explain}",
            netsuite_amount if netsuite_amount is not None else abs_variance, above,
        )
    # 6. fees: fee line on the payout's bank deposit (aggregated per payout at posting)
    if variance_type == "fees":
        return _mk(
            "fees", ACTION_BOOK_FEE_LINE,
            f"Stripe processing fee — book as a fee line on the payout's bank deposit.{explain}",
            abs_variance, above,
        )
    # 7. missing counterpart
    if variance_type == "missing":
        if evidence.get("order_reference"):
            return _mk(
                "missing", ACTION_CREATE_AND_APPLY,
                f"Charge has no NetSuite deposit — create a customer deposit and apply it to the order.{explain}",
                stripe_amount if stripe_amount is not None else abs_variance, above,
            )
        return _mk(
            "missing", ACTION_NEEDS_HUMAN,
            f"Charge has no NetSuite deposit and no order reference — needs investigation.{explain}",
            abs_variance, above,
        )
    # 8. fx/rounding: sub-materiality write-off (flagged JE fallback); material → human
    if variance_type == "fx_rounding":
        if not above:
            return _mk(
                "fx_rounding", ACTION_WRITEOFF_JE,
                f"Sub-materiality FX/rounding difference — aggregate write-off journal.{explain}",
                abs_variance, above,
            )
        return _mk(
            "fx_rounding", ACTION_NEEDS_HUMAN,
            f"FX/rounding variance above materiality — needs investigation.{explain}",
            abs_variance, above,
        )
    # 9. timing: reconciling item, never force-matched, never booked
    if variance_type == "timing":
        return _mk(
            "timing", ACTION_CARRY_FORWARD,
            f"Timing difference — carry forward as a reconciling item; no booking.{explain}",
            abs_variance, above,
        )
    # 10. everything else — the agent tail (Phase 2) / human
    return _mk(
        root, ACTION_NEEDS_HUMAN,
        f"Unexplained variance — needs investigation.{explain}",
        abs_variance, above,
    )
```

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_planner.py -v` — expect PASS (16 tests)**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reconciliation/resolution_planner.py backend/tests/test_resolution_planner.py
git commit -m "feat(recon): deterministic ResolutionPlanner rule engine (10 ordered rules, materiality-decoupled)"
```

---

### Task 4: `plan_run` orchestrator (supersede, guard, chunked insert, audit)

**Files:**
- Modify: `backend/app/services/reconciliation/resolution_planner.py` (append orchestrator)
- Test: `backend/tests/test_resolution_plan_run.py`

**Interfaces:**
- Produces: `async def plan_run(db, tenant_id, run_id) -> dict` returning `{"planned_count", "skipped_guard_count", "superseded_count", "by_action": {...}}`.
- Consumes: `create_test_recon_run` / `create_test_recon_result` conftest factories; `load_materiality` (Task 2 context); `audit_service.log_event`.

- [ ] **Step 1: Write the failing test**

```python
"""plan_run orchestrator: supersede-then-insert, cross-run guard, audit event."""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation.resolution_planner import plan_run
from tests.conftest import create_test_recon_result, create_test_recon_run


async def _result(db, tenant_id, run_id, **over):
    defaults = dict(
        status="pending", bucket="needs_review", match_type="deterministic",
        variance_type="fees", variance_amount=Decimal("3.20"),
        stripe_amount=Decimal("100.00"), netsuite_amount=Decimal("96.80"),
        evidence={"charge_source_id": f"ch_{uuid.uuid4().hex[:8]}", "order_reference": "R123456789"},
    )
    defaults.update(over)
    return await create_test_recon_result(db, tenant_id, run_id, **defaults)


async def test_plan_run_writes_proposals_for_non_matches(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id, variance_type="fees", bucket="auto_classifications")
    await _result(db, tenant_a.id, run.id, variance_type="timing", variance_amount=Decimal("0"), bucket="rules")
    # a clean match must NOT get a proposal
    await _result(db, tenant_a.id, run.id, variance_type=None, variance_amount=Decimal("0"),
                  bucket="matches", status="auto_matched")
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)

    props = (await db.execute(
        select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)
    )).scalars().all()
    assert out["planned_count"] == 2
    assert len(props) == 2
    assert {p.action for p in props} == {"book_fee_line", "carry_forward"}
    assert all(p.status == "proposed" and p.source == "planner" for p in props)
    assert all(p.charge_source_id for p in props)


async def test_plan_run_is_idempotent_via_supersede(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    out2 = await plan_run(db, tenant_a.id, run.id)  # re-plan must not violate the active-unique index

    props = (await db.execute(
        select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)
    )).scalars().all()
    assert out2["superseded_count"] == 1
    assert sorted(p.status for p in props) == ["proposed", "superseded"]


async def test_plan_run_never_supersedes_decided_proposals(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    prop = (await db.execute(
        select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)
    )).scalar_one()
    prop.status = "approved"
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)
    await db.refresh(prop)
    assert prop.status == "approved"  # decided rows untouched
    assert out["planned_count"] == 0  # its result is not re-planned either


async def test_plan_run_cross_run_posted_guard(db, tenant_a):
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    r1 = await _result(db, tenant_a.id, run1.id,
                       evidence={"charge_source_id": "ch_posted", "order_reference": "R1"})
    await db.flush()
    await plan_run(db, tenant_a.id, run1.id)
    p1 = (await db.execute(
        select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == r1.id)
    )).scalar_one()
    p1.status = "posted"
    await db.flush()

    run2 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run2.id,
                  evidence={"charge_source_id": "ch_posted", "order_reference": "R1"})
    await db.flush()
    out = await plan_run(db, tenant_a.id, run2.id)
    assert out["skipped_guard_count"] == 1
    assert out["planned_count"] == 0


async def test_plan_run_emits_summary_audit_event(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    evt = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "recon.resolution.planned",
            AuditEvent.resource_id == str(run.id),
        )
    )).scalars().all()
    assert len(evt) == 1
    assert evt[0].actor_type == "system"
    assert evt[0].payload["planned_count"] == 1
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_plan_run.py -v` — expect FAIL (`plan_run` missing)**

- [ ] **Step 3: Implement (append to `resolution_planner.py`)**

```python
import logging
import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 5000  # Framework-scale runs are tens of thousands of lines


async def plan_run(db: AsyncSession, tenant_id, run_id) -> dict:
    """Plan every non-clean result of *run_id* into resolution proposals.

    Idempotent: existing 'proposed' rows for the run are superseded first;
    decided rows (approved/posted/…) are never touched and their results are
    not re-planned. A per-item mapping error abstains that item (needs_human
    path is total, so this only guards truly unexpected data). Planning failure
    must never fail the run — callers wrap this in try/except.
    """
    from app.models.reconciliation import (
        ACTIVE_PROPOSAL_STATUSES,
        ReconResolutionProposal,
        ReconciliationResult,
        ReconciliationRun,
    )
    from app.services import audit_service
    from app.services.reconciliation.materiality import load_materiality

    tid = tenant_id if isinstance(tenant_id, _uuid.UUID) else _uuid.UUID(str(tenant_id))
    rid = run_id if isinstance(run_id, _uuid.UUID) else _uuid.UUID(str(run_id))

    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == rid, ReconciliationRun.tenant_id == tid
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise ValueError("run not found")

    mat_abs, mat_pct = await load_materiality(db, tid)

    # 1. supersede this run's undecided proposals (re-plan safety; the partial
    #    unique index would otherwise reject the fresh insert).
    superseded_count = (
        await db.execute(
            update(ReconResolutionProposal)
            .where(
                ReconResolutionProposal.run_id == rid,
                ReconResolutionProposal.tenant_id == tid,
                ReconResolutionProposal.status == "proposed",
            )
            .values(status="superseded")
            .execution_options(synchronize_session=False)
        )
    ).rowcount

    # 2. results still holding an ACTIVE proposal (approved/posting/posted/
    #    post_failed) are decided — exclude them from re-planning.
    decided_result_ids = select(ReconResolutionProposal.result_id).where(
        ReconResolutionProposal.run_id == rid,
        ReconResolutionProposal.tenant_id == tid,
        ReconResolutionProposal.status.in_(ACTIVE_PROPOSAL_STATUSES),
    )

    # 3. cross-run double-posting guard: charge ids with a posted proposal
    #    anywhere in this tenant's history.
    posted_charge_ids = set(
        (
            await db.execute(
                select(ReconResolutionProposal.charge_source_id).where(
                    ReconResolutionProposal.tenant_id == tid,
                    ReconResolutionProposal.status == "posted",
                    ReconResolutionProposal.charge_source_id.is_not(None),
                )
            )
        ).scalars().all()
    )

    # 4. column-only select (evidence included — planner reads order_reference).
    rows = (
        await db.execute(
            select(
                ReconciliationResult.id,
                ReconciliationResult.match_type,
                ReconciliationResult.variance_type,
                ReconciliationResult.variance_amount,
                ReconciliationResult.stripe_amount,
                ReconciliationResult.netsuite_amount,
                ReconciliationResult.currency,
                ReconciliationResult.variance_explanation,
                ReconciliationResult.evidence,
            ).where(
                ReconciliationResult.run_id == rid,
                ReconciliationResult.tenant_id == tid,
                ReconciliationResult.bucket != "matches",
                ReconciliationResult.id.notin_(decided_result_ids),
            )
        )
    ).all()

    now = datetime.now(timezone.utc)
    to_insert: list[dict] = []
    skipped_guard = 0
    by_action: dict[str, int] = {}
    for row in rows:
        evidence = row.evidence or {}
        charge_source_id = evidence.get("charge_source_id")
        planned = plan_result(
            match_type=row.match_type,
            variance_type=row.variance_type,
            variance_amount=row.variance_amount,
            stripe_amount=row.stripe_amount,
            netsuite_amount=row.netsuite_amount,
            currency=row.currency,
            variance_explanation=row.variance_explanation,
            evidence=evidence,
            already_posted=charge_source_id in posted_charge_ids if charge_source_id else False,
            materiality_abs=mat_abs,
            materiality_pct=mat_pct,
        )
        if planned is None:
            if charge_source_id in posted_charge_ids:
                skipped_guard += 1
            continue
        by_action[planned.action] = by_action.get(planned.action, 0) + 1
        to_insert.append(
            {
                "tenant_id": tid,
                "run_id": rid,
                "result_id": row.id,
                "root_cause": planned.root_cause,
                "action": planned.action,
                "booking_vehicle": planned.booking_vehicle,
                "group_key": planned.group_key,
                "source": "planner",
                "narrative": planned.narrative,
                "evidence": {"charge_source_id": charge_source_id} if charge_source_id else None,
                "proposed_amount": planned.proposed_amount,
                "currency": row.currency,
                "above_materiality": planned.above_materiality,
                "status": "proposed",
                "charge_source_id": charge_source_id,
                "created_at": now,
                "updated_at": now,
            }
        )

    for i in range(0, len(to_insert), _INSERT_CHUNK):
        await db.execute(insert(ReconResolutionProposal), to_insert[i : i + _INSERT_CHUNK])

    summary = {
        "planned_count": len(to_insert),
        "skipped_guard_count": skipped_guard,
        "superseded_count": superseded_count,
        "by_action": by_action,
    }
    await audit_service.log_event(
        db=db,
        tenant_id=tid,
        category="reconciliation",
        action="recon.resolution.planned",
        actor_id=None,
        actor_type="system",
        resource_type="reconciliation_run",
        resource_id=str(rid),
        correlation_id=f"resolution-plan-{_uuid.uuid4().hex}",
        payload=summary,
    )
    await db.commit()
    return summary
```

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_plan_run.py -v` — expect PASS (5 tests). Check the conftest factory: if `create_test_recon_result` lacks kwargs used above (`stripe_amount`, `netsuite_amount`, `evidence`, `variance_type`, `variance_amount`, `match_type`), extend the factory with those optional kwargs (defaulting to current behavior) rather than changing call sites.**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reconciliation/resolution_planner.py backend/tests/test_resolution_plan_run.py backend/tests/conftest.py
git commit -m "feat(recon): plan_run orchestrator — supersede-then-insert, cross-run posted guard, system audit"
```

---

### Task 5: Post-run hook + retry endpoint

**Files:**
- Modify: `backend/app/services/reconciliation/order_recon_job.py` (after the finalize `await self.db.commit()` at the end of the run step — see `run()` around lines 111-133)
- Modify: `backend/app/api/v1/reconciliation.py` (new endpoint)
- Test: `backend/tests/test_resolution_plan_hook.py`

**Interfaces:**
- Produces: `POST /reconciliation/runs/{run_id}/plan-resolutions` (permission `recon.run`), returns `plan_run`'s summary dict.
- Consumes: `plan_run` (Task 4).

- [ ] **Step 1: Write the failing test**

```python
"""Planner runs automatically post-run and is retryable via endpoint."""

from decimal import Decimal

from sqlalchemy import select

from app.api.v1.reconciliation import plan_resolutions
from app.models.reconciliation import ReconResolutionProposal
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def test_plan_resolutions_endpoint_plans_a_completed_run(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="needs_review",
        match_type="deterministic", variance_type="fees",
        variance_amount=Decimal("3.20"), stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("96.80"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    await db.flush()

    out = await plan_resolutions(str(run.id), user=user, db=db)

    assert out["planned_count"] == 1
    props = (await db.execute(
        select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)
    )).scalars().all()
    assert len(props) == 1


async def test_plan_resolutions_404_on_foreign_run(db, tenant_a, tenant_b):
    user, _ = await create_test_user(db, tenant_a)
    run_b = await create_test_recon_run(db, tenant_b.id, status="completed")
    await db.flush()
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await plan_resolutions(str(run_b.id), user=user, db=db)
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_plan_hook.py -v` — expect FAIL (endpoint missing)**

- [ ] **Step 3: Implement**

In `backend/app/api/v1/reconciliation.py` add (import `plan_run` at top with the other service imports: `from app.services.reconciliation.resolution_planner import plan_run`):

```python
@router.post("/runs/{run_id}/plan-resolutions")
async def plan_resolutions(
    run_id: str,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """(Re-)plan resolution proposals for a run. Idempotent: undecided
    proposals are superseded and re-derived; decided ones are untouched.
    DB-only — never posts to NetSuite."""
    run_uuid = _parse_uuid(run_id)
    try:
        return await plan_run(db, user.tenant_id, run_uuid)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
```

In `backend/app/services/reconciliation/order_recon_job.py`, immediately after the finalize commit in `run()` (the `await self.db.commit()` following the per-bucket rollup assignment — around line 133), add:

```python
            # Phase 1 (summary-first rework): derive resolution proposals.
            # Planning failure must never fail the run — the page offers retry
            # via POST /runs/{run_id}/plan-resolutions.
            try:
                from app.services.reconciliation.resolution_planner import plan_run

                await plan_run(self.db, self.tenant_id, run_id)
            except Exception:
                logger.exception(
                    "resolution_planning_failed", extra={"run_id": str(run_id)}
                )
```

(If `order_recon_job.py` has no module `logger`, add `logger = logging.getLogger(__name__)` + `import logging` following the module's conventions.)

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_plan_hook.py backend/tests/test_order_recon_job.py -v` — expect PASS (hook must not break existing OrderReconJob tests)**

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/reconciliation.py backend/app/services/reconciliation/order_recon_job.py backend/tests/test_resolution_plan_hook.py
git commit -m "feat(recon): auto-plan resolutions post-run + retryable plan-resolutions endpoint"
```

---

### Task 6: Resolution summary + group proposals endpoints

**Files:**
- Modify: `backend/app/api/v1/reconciliation.py`
- Test: `backend/tests/test_resolution_summary_api.py`

**Interfaces:**
- Produces: `GET /reconciliation/runs/{run_id}/resolution-summary` → `ResolutionSummaryResponse`; `GET /reconciliation/runs/{run_id}/resolution-groups/{group_key}/proposals?limit&offset` → `list[ResolutionProposalResponse]`.
- Consumes: schemas (Task 2), proposals written by Tasks 4-5.

- [ ] **Step 1: Write the failing test**

```python
"""resolution-summary aggregation + per-group proposal listing."""

from decimal import Decimal

from app.api.v1.reconciliation import get_resolution_summary, list_group_proposals, plan_resolutions
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def _seed(db, tenant):
    user, _ = await create_test_user(db, tenant)
    run = await create_test_recon_run(db, tenant.id, status="completed")
    # 2 fee lines (one above materiality), 1 timing, 1 chargeback, 1 clean match.
    # Materiality is R2a OR-semantics: above when > $50 abs OR > 1% of order.
    # $9 on $1000 (0.9%) is sub-materiality; $120 on $10000 is above ($120 > $50).
    for amt, stripe in ((Decimal("9.00"), Decimal("1000")), (Decimal("120.00"), Decimal("10000"))):
        await create_test_recon_result(
            db, tenant.id, run.id, status="pending", bucket="auto_classifications",
            match_type="deterministic", variance_type="fees", variance_amount=amt,
            stripe_amount=stripe, netsuite_amount=stripe - amt,
            evidence={"charge_source_id": f"ch_{amt}", "order_reference": "R1"},
        )
    await create_test_recon_result(
        db, tenant.id, run.id, status="pending", bucket="rules",
        match_type="fuzzy", variance_type="timing", variance_amount=Decimal("0"),
        stripe_amount=Decimal("50"), netsuite_amount=Decimal("50"),
        evidence={"charge_source_id": "ch_t", "order_reference": "R2"},
    )
    await create_test_recon_result(
        db, tenant.id, run.id, status="pending", bucket="needs_review",
        match_type="deterministic", variance_type="chargeback", variance_amount=Decimal("42"),
        stripe_amount=Decimal("42"), netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_c", "order_reference": "R3"},
    )
    await create_test_recon_result(
        db, tenant.id, run.id, status="auto_matched", bucket="matches",
        match_type="deterministic", variance_type=None, variance_amount=Decimal("0"),
        stripe_amount=Decimal("10"), netsuite_amount=Decimal("10"),
        evidence={"charge_source_id": "ch_m", "order_reference": "R4"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


async def test_summary_groups_and_rates(db, tenant_a):
    user, run = await _seed(db, tenant_a)
    out = await get_resolution_summary(str(run.id), user=user, db=db)

    assert out.total_results == 5
    assert out.matches_count == 1
    assert out.proposals_count == 4
    # chargeback → needs_human; fees ×2 + timing are "explained"
    assert out.explained_count == 3
    assert out.explained_rate == Decimal("75.0")
    keys = {g.group_key for g in out.groups}
    assert "fees:book_fee_line:deposit" in keys
    assert "timing:carry_forward:none" in keys
    assert "chargeback:needs_human:none" in keys
    fee_group = next(g for g in out.groups if g.root_cause == "fees")
    assert fee_group.count == 2
    assert fee_group.above_materiality_count == 1
    assert fee_group.total_amount == Decimal("129.00")
    assert out.variance_by_root_cause["fees"] == Decimal("129.00")


async def test_group_proposals_listing_paginated(db, tenant_a):
    user, run = await _seed(db, tenant_a)
    page = await list_group_proposals(
        str(run.id), "fees:book_fee_line:deposit", user=user, db=db, limit=1, offset=0
    )
    assert len(page) == 1
    assert page[0].action == "book_fee_line"


async def test_summary_404_on_foreign_run(db, tenant_a, tenant_b):
    user, _ = await create_test_user(db, tenant_a)
    run_b = await create_test_recon_run(db, tenant_b.id, status="completed")
    await db.flush()
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await get_resolution_summary(str(run_b.id), user=user, db=db)
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_summary_api.py -v` — expect FAIL**

- [ ] **Step 3: Implement (add to `reconciliation.py`; extend the schema import block with the new names; import `ReconResolutionProposal`)**

```python
def _parse_group_key(group_key: str) -> tuple[str, str, str]:
    parts = group_key.split(":")
    if len(parts) != 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="group_key must be root_cause:action:booking_vehicle",
        )
    return parts[0], parts[1], parts[2]


async def _get_run_or_404(db: AsyncSession, tenant_id, run_id_str: str) -> ReconciliationRun:
    run_uuid = _parse_uuid(run_id_str)
    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == run_uuid,
                ReconciliationRun.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("/runs/{run_id}/resolution-summary", response_model=ResolutionSummaryResponse)
async def get_resolution_summary(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Summary-first payload: rates + variance-by-root-cause + computed groups.

    Groups are computed here by GROUP BY on real columns (never parsed out of
    group_key). Only ACTIVE, undecided-or-approved proposal states are shown;
    superseded/rejected history is excluded.
    """
    run = await _get_run_or_404(db, user.tenant_id, run_id)
    run_uuid = run.id

    total_results = (
        await db.execute(
            select(func.count(ReconciliationResult.id)).where(
                ReconciliationResult.run_id == run_uuid,
                ReconciliationResult.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one()
    matches_count = run.matches_count

    P = ReconResolutionProposal
    live = (P.run_id == run_uuid, P.tenant_id == user.tenant_id, P.status.notin_(("superseded", "rejected")))

    group_rows = (
        await db.execute(
            select(
                P.root_cause,
                P.action,
                P.booking_vehicle,
                P.group_key,
                func.count(P.id).label("count"),
                func.count(P.id).filter(P.status == "proposed").label("proposed_count"),
                func.count(P.id).filter(P.status == "approved").label("approved_count"),
                func.coalesce(func.sum(P.proposed_amount), 0).label("total_amount"),
                func.count(P.id).filter(P.above_materiality.is_(True)).label("above_materiality_count"),
            )
            .where(*live)
            .group_by(P.root_cause, P.action, P.booking_vehicle, P.group_key)
            .order_by(func.coalesce(func.sum(P.proposed_amount), 0).desc())
        )
    ).all()

    groups = [
        ResolutionGroupSummary(
            group_key=r.group_key,
            root_cause=r.root_cause,
            action=r.action,
            booking_vehicle=r.booking_vehicle,
            count=r.count,
            proposed_count=r.proposed_count,
            approved_count=r.approved_count,
            total_amount=r.total_amount,
            above_materiality_count=r.above_materiality_count,
        )
        for r in group_rows
    ]
    proposals_count = sum(g.count for g in groups)
    explained_count = sum(g.count for g in groups if g.action != "needs_human")
    variance_by_root_cause: dict[str, Decimal] = {}
    for g in groups:
        variance_by_root_cause[g.root_cause] = variance_by_root_cause.get(g.root_cause, Decimal("0")) + g.total_amount

    def _pct(numerator: int, denominator: int) -> Decimal:
        if denominator == 0:
            return Decimal("0")
        return (Decimal(numerator) / Decimal(denominator) * 100).quantize(Decimal("0.1"))

    # guard-skip visibility: results with no live proposal, no match, and a
    # posted proposal elsewhere are reported by plan_run's audit; recompute the
    # cheap upper bound here for the header (results minus matches minus live).
    guard_skipped_count = max(0, total_results - matches_count - proposals_count)

    return ResolutionSummaryResponse(
        run_id=run_id,
        total_results=total_results,
        matches_count=matches_count,
        match_rate=_pct(matches_count, total_results),
        proposals_count=proposals_count,
        explained_count=explained_count,
        explained_rate=_pct(explained_count, proposals_count),
        guard_skipped_count=guard_skipped_count,
        variance_by_root_cause=variance_by_root_cause,
        groups=groups,
    )


@router.get(
    "/runs/{run_id}/resolution-groups/{group_key}/proposals",
    response_model=list[ResolutionProposalResponse],
)
async def list_group_proposals(
    run_id: str,
    group_key: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 100,
    offset: int = 0,
):
    await _get_run_or_404(db, user.tenant_id, run_id)
    root_cause, action, vehicle = _parse_group_key(group_key)
    P = ReconResolutionProposal
    rows = (
        await db.execute(
            select(P)
            .where(
                P.run_id == _parse_uuid(run_id),
                P.tenant_id == user.tenant_id,
                P.root_cause == root_cause,
                P.action == action,
                P.booking_vehicle == vehicle,
                P.status.notin_(("superseded", "rejected")),
            )
            .order_by(P.proposed_amount.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [ResolutionProposalResponse.model_validate(p) for p in rows]
```

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_summary_api.py -v` — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/reconciliation.py backend/tests/test_resolution_summary_api.py
git commit -m "feat(recon): resolution-summary + group-proposals endpoints (computed groups, explained rate)"
```

---

### Task 7: Group approve / reject / proposal override endpoints (+ `carried_forward`)

**Files:**
- Modify: `backend/app/api/v1/reconciliation.py`
- Modify: `backend/app/schemas/reconciliation.py` (`ReconCloseReadiness` gains `carried_forward: int = 0`)
- Test: `backend/tests/test_resolution_group_actions.py`
- Test: `backend/tests/test_close_carried_forward.py`

**Interfaces:**
- Produces: `POST .../resolution-groups/{group_key}/approve` → `ResolutionGroupApproveResult`; `POST .../reject` → `ResolutionGroupRejectResult`; `PATCH /resolution-proposals/{proposal_id}` (body `ResolutionProposalOverride`) → `ResolutionProposalResponse` (the NEW active proposal); close-readiness gains a `carried_forward` count.
- Approve flips results: `carry_forward` group → `carried_forward`; every other approvable action → `approved`. `needs_human` groups return 400.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_resolution_group_actions.py`:

```python
"""Group approve/reject/override — set-based, audited, materiality-capped."""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.reconciliation import (
    approve_resolution_group,
    override_resolution_proposal,
    plan_resolutions,
    reject_resolution_group,
)
from app.models.audit import AuditEvent
from app.models.reconciliation import ReconResolutionProposal, ReconciliationResult
from app.schemas.reconciliation import ResolutionGroupApprove, ResolutionProposalOverride
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def _seed_fees(db, tenant, above_too=True):
    user, _ = await create_test_user(db, tenant)
    run = await create_test_recon_run(db, tenant.id, status="completed")
    # $9 on $1000 = sub-materiality (R2a OR-semantics: not > $50 abs, not > 1%).
    amounts = [(Decimal("9.00"), Decimal("1000"))]
    if above_too:
        amounts.append((Decimal("120.00"), Decimal("10000")))
    for amt, stripe in amounts:
        await create_test_recon_result(
            db, tenant.id, run.id, status="pending", bucket="auto_classifications",
            match_type="deterministic", variance_type="fees", variance_amount=amt,
            stripe_amount=stripe, netsuite_amount=stripe - amt,
            evidence={"charge_source_id": f"ch_{amt}", "order_reference": "R1"},
        )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


async def _props(db, run_id):
    return (await db.execute(
        select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run_id)
    )).scalars().all()


async def test_approve_group_skips_above_materiality_by_default(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a)
    out = await approve_resolution_group(
        str(run.id), "fees:book_fee_line:deposit",
        ResolutionGroupApprove(notes="month-end"), user=user, db=db,
    )
    assert out.approved_count == 1  # only the sub-materiality item
    assert out.skipped_count == 1
    props = await _props(db, run.id)
    by_amount = {p.proposed_amount: p.status for p in props}
    assert by_amount[Decimal("9.00")] == "approved"
    assert by_amount[Decimal("120.00")] == "proposed"


async def test_approve_group_includes_ticked_above_materiality(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a)
    above = next(p for p in await _props(db, run.id) if p.above_materiality)
    out = await approve_resolution_group(
        str(run.id), "fees:book_fee_line:deposit",
        ResolutionGroupApprove(included_above_materiality_ids=[str(above.id)]),
        user=user, db=db,
    )
    assert out.approved_count == 2


async def test_approve_group_flips_result_status_and_audits(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    out = await approve_resolution_group(
        str(run.id), "fees:book_fee_line:deposit",
        ResolutionGroupApprove(), user=user, db=db,
    )
    results = (await db.execute(
        select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)
    )).scalars().all()
    assert all(r.status == "approved" and r.approved_by == user.id for r in results)
    per_line = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "recon.resolution.approve",
            AuditEvent.correlation_id == out.correlation_id,
        )
    )).scalars().all()
    assert len(per_line) == 1
    summary = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "recon.resolution.bulk_approve",
            AuditEvent.correlation_id == out.correlation_id,
        )
    )).scalars().all()
    assert len(summary) == 1


async def test_carry_forward_group_sets_carried_forward_not_approved(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="rules",
        match_type="fuzzy", variance_type="timing", variance_amount=Decimal("0"),
        stripe_amount=Decimal("50"), netsuite_amount=Decimal("50"),
        evidence={"charge_source_id": "ch_t", "order_reference": "R2"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    await approve_resolution_group(
        str(run.id), "timing:carry_forward:none", ResolutionGroupApprove(), user=user, db=db,
    )
    await db.refresh(r)
    assert r.status == "carried_forward"


async def test_needs_human_group_not_approvable(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="needs_review",
        match_type="deterministic", variance_type="chargeback", variance_amount=Decimal("42"),
        stripe_amount=Decimal("42"), netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_c"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    with pytest.raises(HTTPException) as exc:
        await approve_resolution_group(
            str(run.id), "chargeback:needs_human:none", ResolutionGroupApprove(), user=user, db=db,
        )
    assert exc.value.status_code == 400


async def test_approve_rejected_on_closed_run(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    run.status = "closed"
    await db.flush()
    with pytest.raises(HTTPException) as exc:
        await approve_resolution_group(
            str(run.id), "fees:book_fee_line:deposit", ResolutionGroupApprove(), user=user, db=db,
        )
    assert exc.value.status_code == 400


async def test_reject_group_leaves_results_untouched(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    out = await reject_resolution_group(
        str(run.id), "fees:book_fee_line:deposit", user=user, db=db,
    )
    assert out.rejected_count == 1
    result = (await db.execute(
        select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)
    )).scalar_one()
    assert result.status == "pending"  # result untouched; proposal history retained


async def test_override_supersedes_and_creates_new_active(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    prop = (await _props(db, run.id))[0]
    new = await override_resolution_proposal(
        str(prop.id), ResolutionProposalOverride(action="needs_human", notes="not a fee"),
        user=user, db=db,
    )
    await db.refresh(prop)
    assert prop.status == "superseded"
    assert new.action == "needs_human"
    assert new.source == "human"
    assert new.result_id == str(prop.result_id)
```

`backend/tests/test_close_carried_forward.py`:

```python
"""carried_forward is terminal, non-blocking for close, and never locked."""

from decimal import Decimal

from app.api.v1.reconciliation import approve_bucket, close_period, get_close_readiness
from app.schemas.reconciliation import ReconBucketApprove
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def test_carried_forward_unblocks_readiness_and_is_not_locked(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await create_test_recon_result(
        db, tenant_a.id, run.id, status="carried_forward", bucket="rules",
        match_type="fuzzy", variance_type="timing", variance_amount=Decimal("0"),
    )
    await db.flush()

    readiness = await get_close_readiness("2026-04", user=user, db=db)
    assert readiness.open_exceptions == 0        # not pending → not blocking
    assert readiness.carried_forward == 1        # visible as its own count

    resp = await close_period("2026-04", user=user, db=db)
    await db.refresh(r)
    assert r.status == "carried_forward"          # never locked
    assert resp["results_locked"] == 0


async def test_bulk_approve_skips_carried_forward(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await create_test_recon_result(
        db, tenant_a.id, run.id, status="carried_forward", bucket="rules",
        match_type="fuzzy", variance_type="timing", variance_amount=Decimal("0"),
    )
    await db.flush()
    out = await approve_bucket(str(run.id), ReconBucketApprove(bucket="rules"), user=user, db=db)
    await db.refresh(r)
    assert r.status == "carried_forward"          # TERMINAL_RESULT_STATUSES skip
    assert out.approved_count == 0
```

- [ ] **Step 2: Run both files — expect FAIL**

- [ ] **Step 3: Implement (add to `reconciliation.py`)**

```python
@router.post(
    "/runs/{run_id}/resolution-groups/{group_key}/approve",
    response_model=ResolutionGroupApproveResult,
)
async def approve_resolution_group(
    run_id: str,
    group_key: str,
    request: ResolutionGroupApprove,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Set-based approve of a resolution group. DB-only in Phase 1 (no posting).

    Above-materiality proposals approve ONLY when explicitly ticked
    (included_above_materiality_ids). carry_forward groups flip results to
    'carried_forward'; every other approvable action flips to 'approved'.
    needs_human groups are never group-approvable.
    """
    root_cause, action, vehicle = _parse_group_key(group_key)
    if action == "needs_human":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="needs_human groups must be resolved individually",
        )
    run = await _get_run_or_404(db, user.tenant_id, run_id)
    if run.status in ("closed", "locked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period is closed; cannot approve.",
        )

    now = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    P = ReconResolutionProposal
    included = {_parse_uuid(i) for i in request.included_above_materiality_ids}
    excluded = {_parse_uuid(i) for i in request.excluded_ids}

    group_filter = (
        P.run_id == run.id,
        P.tenant_id == user.tenant_id,
        P.root_cause == root_cause,
        P.action == action,
        P.booking_vehicle == vehicle,
    )
    eligibility = or_(P.above_materiality.is_(False), P.id.in_(included)) if included else P.above_materiality.is_(False)

    upd = (
        update(P)
        .where(
            *group_filter,
            P.status == "proposed",
            eligibility,
            P.id.notin_(excluded) if excluded else sa_true(),
        )
        .values(status="approved", decided_by=user.id, decided_at=now, correlation_id=correlation_id)
        .returning(P.id, P.result_id)
    )
    approved_rows = (await db.execute(upd)).all()
    approved_ids = [r.id for r in approved_rows]
    result_ids = [r.result_id for r in approved_rows]

    total_in_group = (
        await db.execute(select(func.count(P.id)).where(*group_filter, P.status.notin_(("superseded", "rejected"))))
    ).scalar_one()
    skipped_count = total_in_group - len(approved_ids)

    # Flip the underlying results (never terminal rows).
    if result_ids:
        result_status = "carried_forward" if action == "carry_forward" else "approved"
        values = {"status": result_status}
        if result_status == "approved":
            values.update(approved_by=user.id, approved_at=now)
        await db.execute(
            update(ReconciliationResult)
            .where(
                ReconciliationResult.id.in_(result_ids),
                ReconciliationResult.tenant_id == user.tenant_id,
                ReconciliationResult.status.notin_(TERMINAL_RESULT_STATUSES),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    if approved_ids:
        await db.execute(
            insert(AuditEvent),
            [
                {
                    "tenant_id": user.tenant_id,
                    "actor_id": user.id,
                    "actor_type": "user",
                    "category": "reconciliation",
                    "action": "recon.resolution.approve",
                    "resource_type": "recon_resolution_proposal",
                    "resource_id": str(pid),
                    "correlation_id": correlation_id,
                    "status": "success",
                }
                for pid in approved_ids
            ],
        )
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.resolution.bulk_approve",
        actor_id=user.id,
        resource_type="reconciliation_run",
        resource_id=run_id,
        correlation_id=correlation_id,
        payload={"group_key": group_key, "approved_count": len(approved_ids), "notes": request.notes},
    )
    await db.commit()
    return ResolutionGroupApproveResult(
        run_id=run_id,
        group_key=group_key,
        approved_count=len(approved_ids),
        skipped_count=skipped_count,
        correlation_id=correlation_id,
    )


@router.post(
    "/runs/{run_id}/resolution-groups/{group_key}/reject",
    response_model=ResolutionGroupRejectResult,
)
async def reject_resolution_group(
    run_id: str,
    group_key: str,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Reject a whole group's undecided proposals. Results are untouched —
    proposal history (incl. gathered evidence) is retained for the human."""
    root_cause, action, vehicle = _parse_group_key(group_key)
    run = await _get_run_or_404(db, user.tenant_id, run_id)
    now = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    P = ReconResolutionProposal
    rejected_ids = (
        await db.execute(
            update(P)
            .where(
                P.run_id == run.id,
                P.tenant_id == user.tenant_id,
                P.root_cause == root_cause,
                P.action == action,
                P.booking_vehicle == vehicle,
                P.status == "proposed",
            )
            .values(status="rejected", decided_by=user.id, decided_at=now, correlation_id=correlation_id)
            .returning(P.id)
        )
    ).scalars().all()
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.resolution.bulk_reject",
        actor_id=user.id,
        resource_type="reconciliation_run",
        resource_id=run_id,
        correlation_id=correlation_id,
        payload={"group_key": group_key, "rejected_count": len(rejected_ids)},
    )
    await db.commit()
    return ResolutionGroupRejectResult(
        run_id=run_id, group_key=group_key,
        rejected_count=len(rejected_ids), correlation_id=correlation_id,
    )


@router.patch("/resolution-proposals/{proposal_id}", response_model=ResolutionProposalResponse)
async def override_resolution_proposal(
    proposal_id: str,
    request: ResolutionProposalOverride,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Override one proposal's action: supersede the original, create a new
    active proposal (source='human') for the same result. Preserves the
    one-active-proposal invariant and the audit chain."""
    from app.services.reconciliation.resolution_planner import VEHICLE_BY_ACTION, group_key_for

    P = ReconResolutionProposal
    prop = (
        await db.execute(
            select(P).where(P.id == _parse_uuid(proposal_id), P.tenant_id == user.tenant_id)
        )
    ).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    if prop.status != "proposed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only undecided proposals can be overridden (status={prop.status})",
        )
    now = datetime.now(timezone.utc)
    prop.status = "superseded"
    prop.decided_by = user.id
    prop.decided_at = now
    vehicle = VEHICLE_BY_ACTION[request.action]
    note = f" Override note: {request.notes}" if request.notes else ""
    new = ReconResolutionProposal(
        id=uuid.uuid4(),
        tenant_id=user.tenant_id,
        run_id=prop.run_id,
        result_id=prop.result_id,
        root_cause=prop.root_cause,
        action=request.action,
        booking_vehicle=vehicle,
        group_key=group_key_for(prop.root_cause, request.action, vehicle),
        source="human",
        narrative=f"Overridden by user from '{prop.action}'.{note}",
        evidence=prop.evidence,
        proposed_amount=prop.proposed_amount,
        currency=prop.currency,
        above_materiality=prop.above_materiality,
        status="proposed",
        charge_source_id=prop.charge_source_id,
    )
    db.add(new)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.resolution.override",
        actor_id=user.id,
        resource_type="recon_resolution_proposal",
        resource_id=str(prop.id),
        payload={"from_action": prop.action, "to_action": request.action, "notes": request.notes},
    )
    await db.commit()
    await db.refresh(new)
    return ResolutionProposalResponse.model_validate(new)
```

Notes for the implementer:
- `sa_true()` — import as `from sqlalchemy import true as sa_true` (used to no-op the excluded filter when the list is empty). If you prefer, build the `.where()` clause list conditionally instead; either is fine, but do NOT pass `P.id.notin_(set())` unconditionally (SQLAlchemy renders `id NOT IN (NULL)` semantics on empty sets in some dialects).
- Close-readiness: in `get_close_readiness`, add one aggregate alongside the others: `func.count().filter(ReconciliationResult.status == "carried_forward").label("carried_forward")`, thread it into the response, and add `carried_forward: int = 0` to `ReconCloseReadiness` (default keeps old API clients working).
- `close_period`'s `lock_predicate` needs NO change — `carried_forward` matches neither branch and therefore never locks (asserted by the test).

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_group_actions.py backend/tests/test_close_carried_forward.py backend/tests/test_recon_bucket_reviewer.py backend/tests/test_reconciliation_api.py -v` — expect PASS (new + regression)**

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/reconciliation.py backend/app/schemas/reconciliation.py backend/tests/test_resolution_group_actions.py backend/tests/test_close_carried_forward.py
git commit -m "feat(recon): group approve/reject/override endpoints; carried_forward close semantics"
```

---

### Task 8: Feature flag + evidence pack Proposals sheet

**Files:**
- Modify: `backend/app/services/feature_flag_service.py` (DEFAULT_FLAGS)
- Modify: `backend/app/services/reconciliation/evidence_service.py`
- Modify: `backend/app/api/v1/reconciliation.py` (`download_evidence` passes proposals)
- Test: `backend/tests/test_resolution_flag_and_evidence.py`

**Interfaces:**
- Produces: flag key `recon_resolution_ui` (default False); `EvidencePackGenerator.generate(..., proposals: list[dict] | None = None)` adds a "Proposals" sheet when provided.

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

from openpyxl import load_workbook

from app.services.feature_flag_service import DEFAULT_FLAGS
from app.services.reconciliation.evidence_service import EvidencePackGenerator


def test_recon_resolution_ui_flag_registered_default_off():
    assert DEFAULT_FLAGS.get("recon_resolution_ui") is False


def test_proposals_sheet_writer():
    from openpyxl import Workbook

    gen = EvidencePackGenerator()
    proposals = [
        {
            "group_key": "fees:book_fee_line:deposit",
            "root_cause": "fees", "action": "book_fee_line",
            "booking_vehicle": "deposit", "status": "proposed",
            "narrative": "Stripe processing fee — book as a fee line.",
            "proposed_amount": Decimal("3.20"), "currency": "USD",
            "above_materiality": False, "source": "planner",
        }
    ]
    wb = Workbook()
    gen._write_proposals_sheet(wb, proposals)
    assert "Proposals" in wb.sheetnames
    ws = wb["Proposals"]
    headers = [c.value for c in ws[1]]
    assert "Group" in headers and "Action" in headers and "Narrative" in headers
    assert ws.max_row == 2  # header + one proposal row
```

(`load_workbook` import is then unneeded — drop it. The endpoint-level wiring —
`download_evidence` querying non-superseded proposals and passing `proposals=` into
`generate()` — is asserted in Step 3 item 3 by extending ONE existing evidence
endpoint test: add a planned proposal to its seeded run and assert the returned
workbook contains the "Proposals" sheet. Locate that test via
`grep -rl "evidence" backend/tests/`.)

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

1. `feature_flag_service.py` — append to `DEFAULT_FLAGS` with comment:

```python
    # Phase 1 of the summary-first recon rework (spec 2026-07-06): gates the
    # redesigned resolution-groups page surface, independent of posting.
    "recon_resolution_ui": False,
```

2. `evidence_service.py` — read the existing `generate(...)` signature first, then: add optional `proposals: list[dict] | None = None` parameter; after the Exceptions sheet is built, when `proposals` is non-empty append:

```python
    def _write_proposals_sheet(self, wb, proposals: list[dict]) -> None:
        ws = wb.create_sheet("Proposals")
        headers = [
            "Group", "Root Cause", "Action", "Vehicle", "Status", "Source",
            "Amount", "Currency", "Above Materiality", "Narrative",
        ]
        ws.append(headers)
        for p in proposals:
            ws.append([
                p.get("group_key"), p.get("root_cause"), p.get("action"),
                p.get("booking_vehicle"), p.get("status"), p.get("source"),
                float(p.get("proposed_amount") or 0), p.get("currency"),
                "YES" if p.get("above_materiality") else "no", p.get("narrative"),
            ])
```

   Also add a small `generate_smoke_for_tests(proposals)` classmethod ONLY if the real `generate()` needs heavyweight fixtures; prefer calling the real `generate()` in the test with the same dict-shaped results the existing evidence tests use (check for an existing evidence test to copy fixtures from — `backend/tests/` grep "evidence"). If real fixtures are simple enough, delete `generate_smoke_for_tests` from the test and call `generate()` directly. The assertion (Proposals sheet + headers) stays identical.

3. `download_evidence` endpoint: query the run's non-superseded proposals as dicts (same conversion style the endpoint already uses for results) and pass `proposals=`.

- [ ] **Step 4: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_flag_and_evidence.py -v` plus any existing evidence tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/feature_flag_service.py backend/app/services/reconciliation/evidence_service.py backend/app/api/v1/reconciliation.py backend/tests/test_resolution_flag_and_evidence.py
git commit -m "feat(recon): recon_resolution_ui flag (default off) + evidence pack Proposals sheet"
```

---

### Task 9: Frontend types + hooks

**Files:**
- Modify: `frontend/src/lib/types.ts` (append)
- Create: `frontend/src/hooks/use-resolution.ts`
- Test: hooks are exercised through the component tests in Tasks 10-11 (no standalone hook test — matches repo convention).

**Interfaces:**
- Produces: types `ReconResolutionGroup`, `ReconResolutionSummary`, `ReconResolutionProposal`; hooks `useResolutionSummary(runId)`, `useGroupProposals(runId, groupKey, enabled)`, `useApproveResolutionGroup(runId)`, `useRejectResolutionGroup(runId)`, `usePlanResolutions(runId)`.

- [ ] **Step 1: Append types to `frontend/src/lib/types.ts`**

```typescript
// ── Recon resolution proposals (Phase 1, spec 2026-07-06) ──────────────────
export interface ReconResolutionGroup {
  group_key: string;
  root_cause: string;
  action: string;
  booking_vehicle: string;
  count: number;
  proposed_count: number;
  approved_count: number;
  total_amount: string;
  above_materiality_count: number;
}

export interface ReconResolutionSummary {
  run_id: string;
  total_results: number;
  matches_count: number;
  match_rate: string;
  proposals_count: number;
  explained_count: number;
  explained_rate: string;
  guard_skipped_count: number;
  variance_by_root_cause: Record<string, string>;
  groups: ReconResolutionGroup[];
}

export interface ReconResolutionProposal {
  id: string;
  run_id: string;
  result_id: string;
  root_cause: string;
  action: string;
  booking_vehicle: string;
  group_key: string;
  source: string;
  narrative: string;
  proposed_amount: string;
  currency: string;
  above_materiality: boolean;
  status: string;
  failure_reason: string | null;
  correlation_id: string | null;
  created_at: string;
}
```

- [ ] **Step 2: Create `frontend/src/hooks/use-resolution.ts`** (mirrors `use-reconciliation.ts` conventions exactly)

```typescript
"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  ReconResolutionProposal,
  ReconResolutionSummary,
} from "@/lib/types";

export function useResolutionSummary(runId: string | null) {
  return useQuery<ReconResolutionSummary>({
    queryKey: ["recon-resolution-summary", runId],
    queryFn: () =>
      apiClient.get<ReconResolutionSummary>(
        `/api/v1/reconciliation/runs/${runId}/resolution-summary`
      ),
    enabled: !!runId,
  });
}

export function useGroupProposals(
  runId: string | null,
  groupKey: string | null
) {
  return useQuery<ReconResolutionProposal[]>({
    queryKey: ["recon-group-proposals", runId, groupKey],
    queryFn: () =>
      apiClient.get<ReconResolutionProposal[]>(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/${encodeURIComponent(
          groupKey!
        )}/proposals`
      ),
    enabled: !!runId && !!groupKey,
  });
}

function invalidateResolution(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["recon-resolution-summary"] });
  queryClient.invalidateQueries({ queryKey: ["recon-group-proposals"] });
  queryClient.invalidateQueries({ queryKey: ["recon-results"] });
  queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
  // Group approval flips result statuses → the period readiness changes.
  queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
}

export function useApproveResolutionGroup(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      group_key: string;
      notes?: string;
      included_above_materiality_ids?: string[];
      excluded_ids?: string[];
    }) =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/${encodeURIComponent(
          data.group_key
        )}/approve`,
        {
          notes: data.notes,
          included_above_materiality_ids: data.included_above_materiality_ids ?? [],
          excluded_ids: data.excluded_ids ?? [],
        }
      ),
    onSuccess: () => invalidateResolution(queryClient),
  });
}

export function useRejectResolutionGroup(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { group_key: string }) =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/${encodeURIComponent(
          data.group_key
        )}/reject`,
        {}
      ),
    onSuccess: () => invalidateResolution(queryClient),
  });
}

export function usePlanResolutions(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/plan-resolutions`,
        {}
      ),
    onSuccess: () => invalidateResolution(queryClient),
  });
}
```

- [ ] **Step 3: Typecheck: `cd frontend && npx tsc --noEmit` — expect no new errors**

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/hooks/use-resolution.ts
git commit -m "feat(recon-fe): resolution summary/group types + react-query hooks"
```

---

### Task 10: `ResolutionSummaryHeader` + `ResolutionGroupCard` components

**Files:**
- Create: `frontend/src/components/reconciliation/resolution-summary-header.tsx`
- Create: `frontend/src/components/reconciliation/resolution-group-card.tsx`
- Test: `frontend/src/components/reconciliation/__tests__/resolution-group-card.test.tsx`
- Test: `frontend/src/components/reconciliation/__tests__/resolution-summary-header.test.tsx`

**Interfaces:**
- `ResolutionSummaryHeader({ summary: ReconResolutionSummary | null })`
- `ResolutionGroupCard({ group, onApprove(notes, includedAboveIds), onReject, isApproving, disabled, expanded, onToggleExpand, children })` — `children` renders the drill-down items (Task 11).

- [ ] **Step 1: Write the failing tests**

`resolution-group-card.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResolutionGroupCard } from "@/components/reconciliation/resolution-group-card";
import type { ReconResolutionGroup } from "@/lib/types";

const feeGroup: ReconResolutionGroup = {
  group_key: "fees:book_fee_line:deposit",
  root_cause: "fees",
  action: "book_fee_line",
  booking_vehicle: "deposit",
  count: 212,
  proposed_count: 212,
  approved_count: 0,
  total_amount: "1284.55",
  above_materiality_count: 3,
};

const base = {
  onApprove: vi.fn(),
  onReject: vi.fn(),
  isApproving: false,
  expanded: false,
  onToggleExpand: vi.fn(),
};

describe("ResolutionGroupCard", () => {
  it("renders count, amount, and the booking-vehicle chip", () => {
    render(<ResolutionGroupCard group={feeGroup} {...base} />);
    expect(screen.getByText(/212 items/i)).toBeInTheDocument();
    expect(screen.getByText(/\$1,284\.55/)).toBeInTheDocument();
    expect(screen.getByText(/deposit fee line/i)).toBeInTheDocument();
  });

  it("shows the materiality split note when items are above threshold", () => {
    render(<ResolutionGroupCard group={feeGroup} {...base} />);
    expect(screen.getByText(/3 above materiality/i)).toBeInTheDocument();
  });

  it("renders the JE fallback chip as flagged (amber)", () => {
    const je = { ...feeGroup, group_key: "fx_rounding:writeoff_je:journalentry",
      root_cause: "fx_rounding", action: "writeoff_je", booking_vehicle: "journalentry" };
    render(<ResolutionGroupCard group={je} {...base} />);
    const chip = screen.getByText(/journal entry/i);
    expect(chip.className).toContain("amber");
  });

  it("approves with the typed note (only sub-materiality by default)", () => {
    const onApprove = vi.fn();
    render(<ResolutionGroupCard group={feeGroup} {...base} onApprove={onApprove} />);
    fireEvent.change(screen.getByPlaceholderText(/note/i), { target: { value: "close" } });
    fireEvent.click(screen.getByRole("button", { name: /approve 209/i }));
    expect(onApprove).toHaveBeenCalledWith("close", []);
  });

  it("needs_human groups have no approve button", () => {
    const nh = { ...feeGroup, group_key: "chargeback:needs_human:none",
      root_cause: "chargeback", action: "needs_human", booking_vehicle: "none" };
    render(<ResolutionGroupCard group={nh} {...base} />);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByText(/review individually/i)).toBeInTheDocument();
  });

  it("carry_forward groups say acknowledge, not approve", () => {
    const cf = { ...feeGroup, group_key: "timing:carry_forward:none",
      root_cause: "timing", action: "carry_forward", booking_vehicle: "none",
      above_materiality_count: 0 };
    render(<ResolutionGroupCard group={cf} {...base} />);
    expect(screen.getByRole("button", { name: /acknowledge/i })).toBeInTheDocument();
  });
});
```

`resolution-summary-header.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResolutionSummaryHeader } from "@/components/reconciliation/resolution-summary-header";
import type { ReconResolutionSummary } from "@/lib/types";

const summary: ReconResolutionSummary = {
  run_id: "r1",
  total_results: 1000,
  matches_count: 900,
  match_rate: "90.0",
  proposals_count: 100,
  explained_count: 75,
  explained_rate: "75.0",
  guard_skipped_count: 0,
  variance_by_root_cause: { fees: "1284.55", timing: "0.00" },
  groups: [],
};

describe("ResolutionSummaryHeader", () => {
  it("renders match rate, explained rate, and root-cause breakdown", () => {
    render(<ResolutionSummaryHeader summary={summary} />);
    expect(screen.getByText(/90\.0%/)).toBeInTheDocument();
    expect(screen.getByText(/75\.0%/)).toBeInTheDocument();
    expect(screen.getByText(/fees/i)).toBeInTheDocument();
    expect(screen.getByText(/\$1,284\.55/)).toBeInTheDocument();
  });

  it("renders the empty state when summary is null", () => {
    render(<ResolutionSummaryHeader summary={null} />);
    expect(screen.getByText(/no reconciliation run selected/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run: `cd frontend && npx vitest run src/components/reconciliation/__tests__/resolution-group-card.test.tsx src/components/reconciliation/__tests__/resolution-summary-header.test.tsx` — expect FAIL (components missing)**

- [ ] **Step 3: Implement**

`resolution-summary-header.tsx` (style: mirror `recon-summary-bar.tsx` card grid exactly — `rounded-xl border bg-card p-5 shadow-soft`):

```tsx
"use client";

import { CheckCircle2, Lightbulb, DollarSign } from "lucide-react";
import type { ReconResolutionSummary } from "@/lib/types";

interface ResolutionSummaryHeaderProps {
  summary: ReconResolutionSummary | null;
}

const money = (v: string | number) =>
  Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });

export function ResolutionSummaryHeader({ summary }: ResolutionSummaryHeaderProps) {
  if (!summary) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft text-center text-muted-foreground">
        No reconciliation run selected. Start a new run or select a previous one.
      </div>
    );
  }
  const totalVariance = Object.values(summary.variance_by_root_cause).reduce(
    (acc, v) => acc + Number(v),
    0
  );
  const cards = [
    {
      label: "Match rate",
      value: `${summary.match_rate}%`,
      sub: `${summary.matches_count.toLocaleString()} of ${summary.total_results.toLocaleString()} lines`,
      icon: CheckCircle2,
      color: "text-green-600",
      bg: "bg-green-50",
    },
    {
      label: "Explained rate",
      value: `${summary.explained_rate}%`,
      sub: `${summary.explained_count.toLocaleString()} of ${summary.proposals_count.toLocaleString()} exceptions have a proposed resolution`,
      icon: Lightbulb,
      color: "text-indigo-600",
      bg: "bg-indigo-50",
    },
    {
      label: "Gross exception amount",
      value: money(totalVariance),
      sub: "sum of proposed resolution amounts",
      icon: DollarSign,
      color: "text-blue-600",
      bg: "bg-blue-50",
    },
  ];
  const rootCauses = Object.entries(summary.variance_by_root_cause).sort(
    (a, b) => Number(b[1]) - Number(a[1])
  );
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-4">
        {cards.map(({ label, value, sub, icon: Icon, color, bg }) => (
          <div key={label} className="rounded-xl border bg-card p-5 shadow-soft">
            <div className="flex items-center gap-3">
              <div className={`rounded-lg ${bg} p-2`}>
                <Icon className={`h-5 w-5 ${color}`} />
              </div>
              <div>
                <p className="text-[13px] text-muted-foreground">{label}</p>
                <p className="text-2xl font-semibold text-foreground">{value}</p>
                <p className="text-xs text-muted-foreground">{sub}</p>
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {rootCauses.map(([cause, amount]) => (
          <span
            key={cause}
            className="inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1 text-[13px]"
          >
            <span className="font-medium">{cause}</span>
            <span className="text-muted-foreground">{money(amount)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
```

`resolution-group-card.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, XCircle } from "lucide-react";
import type { ReconResolutionGroup } from "@/lib/types";

// Booking-vehicle chip copy + styling. journalentry is the flagged fallback —
// finance must SEE what is being booked as a raw JE (spec: amber chip).
const VEHICLE_CHIP: Record<string, { label: string; className: string }> = {
  deposit: { label: "Deposit fee line", className: "bg-blue-50 text-blue-700 border-blue-200" },
  customerdeposit: { label: "Customer deposit", className: "bg-blue-50 text-blue-700 border-blue-200" },
  depositapplication: { label: "Deposit application", className: "bg-blue-50 text-blue-700 border-blue-200" },
  creditmemo: { label: "Credit memo + refund", className: "bg-blue-50 text-blue-700 border-blue-200" },
  journalentry: { label: "Journal entry (fallback)", className: "bg-amber-50 text-amber-700 border-amber-300" },
  none: { label: "No booking", className: "bg-muted text-muted-foreground border-transparent" },
};

const ROOT_CAUSE_LABEL: Record<string, string> = {
  fees: "Stripe processing fees",
  missing: "Missing NetSuite deposit",
  fx_rounding: "FX / rounding",
  timing: "Timing differences",
  duplicate: "Duplicate deposits",
  chargeback: "Chargebacks / disputes",
  manual_adjustment: "Unexplained",
};

interface ResolutionGroupCardProps {
  group: ReconResolutionGroup;
  onApprove: (notes: string, includedAboveIds: string[]) => void;
  onReject: () => void;
  isApproving: boolean;
  disabled?: boolean;
  expanded: boolean;
  onToggleExpand: () => void;
  // Ticked above-materiality proposal ids, owned by the drill-down (children).
  includedAboveIds?: string[];
  children?: React.ReactNode;
  resetSignal?: number;
}

export function ResolutionGroupCard({
  group,
  onApprove,
  onReject,
  isApproving,
  disabled,
  expanded,
  onToggleExpand,
  includedAboveIds = [],
  children,
  resetSignal,
}: ResolutionGroupCardProps) {
  const [notes, setNotes] = useState("");
  useEffect(() => {
    setNotes("");
  }, [resetSignal]);

  const money = Number(group.total_amount).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
  });
  const chip = VEHICLE_CHIP[group.booking_vehicle] ?? VEHICLE_CHIP.none;
  const isNeedsHuman = group.action === "needs_human";
  const isCarryForward = group.action === "carry_forward";
  const oneClickCount =
    group.proposed_count - group.above_materiality_count + includedAboveIds.length;
  const blocked = disabled || isApproving || oneClickCount <= 0;

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-start justify-between gap-4">
        <button type="button" onClick={onToggleExpand} className="flex items-start gap-2 text-left">
          {expanded ? (
            <ChevronDown className="mt-1 h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-1 h-4 w-4 text-muted-foreground" />
          )}
          <div>
            <div className="flex items-center gap-2">
              <p className="text-[15px] font-medium text-foreground">
                {ROOT_CAUSE_LABEL[group.root_cause] ?? group.root_cause}
              </p>
              <span className={`rounded-full border px-2 py-0.5 text-xs ${chip.className}`}>
                {chip.label}
              </span>
            </div>
            <p className="text-[13px] text-muted-foreground">
              {group.count.toLocaleString()} items · {money}
              {group.approved_count > 0 && ` · ${group.approved_count} already approved`}
            </p>
            {group.above_materiality_count > 0 && !isNeedsHuman && (
              <p className="mt-1 text-xs text-amber-700">
                {group.above_materiality_count} above materiality — tick them individually in the item list.
              </p>
            )}
          </div>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          {isNeedsHuman ? (
            <span className="text-[13px] text-muted-foreground">Review individually</span>
          ) : (
            <>
              <button
                type="button"
                onClick={onReject}
                disabled={disabled || isApproving}
                className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              >
                <XCircle className="h-4 w-4" />
                Reject
              </button>
              <button
                type="button"
                onClick={() => onApprove(notes, includedAboveIds)}
                disabled={blocked}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <CheckCircle2 className="h-4 w-4" />
                {isApproving
                  ? "Working…"
                  : isCarryForward
                    ? `Acknowledge ${oneClickCount}`
                    : `Approve ${oneClickCount}`}
              </button>
            </>
          )}
        </div>
      </div>
      {!isNeedsHuman && (
        <input
          type="text"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          disabled={blocked}
          placeholder="Optional note for the audit trail (e.g. month-end close)"
          className="mt-3 w-full rounded-md border bg-background px-3 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
        />
      )}
      {expanded && <div className="mt-4 border-t pt-4">{children}</div>}
    </div>
  );
}
```

- [ ] **Step 4: Run: `cd frontend && npx vitest run src/components/reconciliation/__tests__/` — expect PASS (new + existing bulk-approval-card tests)**

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/reconciliation/resolution-summary-header.tsx frontend/src/components/reconciliation/resolution-group-card.tsx frontend/src/components/reconciliation/__tests__/resolution-group-card.test.tsx frontend/src/components/reconciliation/__tests__/resolution-summary-header.test.tsx
git commit -m "feat(recon-fe): resolution summary header + group card (amber JE chip, materiality split)"
```

---

### Task 11: Group items drill-down + page integration behind the flag

**Files:**
- Create: `frontend/src/components/reconciliation/resolution-group-items.tsx`
- Modify: `frontend/src/app/(dashboard)/reconciliation/page.tsx`
- Test: `frontend/src/components/reconciliation/__tests__/resolution-group-items.test.tsx`

**Interfaces:**
- `ResolutionGroupItems({ runId, groupKey, onTickAbove(id, ticked), tickedAboveIds, onInvestigate(proposal) })` — fetches via `useGroupProposals`, renders item rows with narrative + amount; above-materiality rows get a checkbox; `needs_human` rows get "Investigate in chat".
- Page: `useFeature("recon_resolution_ui")` branches the surface. Flag OFF → the existing tabs/table/cards block renders EXACTLY as today (no visual diff). Flag ON → `ResolutionSummaryHeader` + group card list + a collapsible "All results (classic view)" section that reuses the existing tab block.

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResolutionGroupItems } from "@/components/reconciliation/resolution-group-items";
import type { ReconResolutionProposal } from "@/lib/types";

const proposals: ReconResolutionProposal[] = [
  {
    id: "p1", run_id: "r1", result_id: "res1",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "Stripe processing fee — book as a fee line.",
    proposed_amount: "3.20", currency: "USD",
    above_materiality: false, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
  },
  {
    id: "p2", run_id: "r1", result_id: "res2",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "Large fee variance.",
    proposed_amount: "120.00", currency: "USD",
    above_materiality: true, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
  },
];

vi.mock("@/hooks/use-resolution", () => ({
  useGroupProposals: () => ({ data: proposals, isLoading: false }),
}));

describe("ResolutionGroupItems", () => {
  const base = {
    runId: "r1",
    groupKey: "fees:book_fee_line:deposit",
    tickedAboveIds: [] as string[],
    onTickAbove: vi.fn(),
    onInvestigate: vi.fn(),
  };

  it("renders narratives and amounts", () => {
    render(<ResolutionGroupItems {...base} />);
    expect(screen.getByText(/book as a fee line/i)).toBeInTheDocument();
    expect(screen.getByText(/\$120\.00/)).toBeInTheDocument();
  });

  it("only above-materiality rows get an inclusion checkbox", () => {
    render(<ResolutionGroupItems {...base} />);
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  });

  it("ticking the checkbox reports the proposal id", () => {
    const onTickAbove = vi.fn();
    render(<ResolutionGroupItems {...base} onTickAbove={onTickAbove} />);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onTickAbove).toHaveBeenCalledWith("p2", true);
  });
});
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

`resolution-group-items.tsx`:

```tsx
"use client";

import { MessageSquare } from "lucide-react";
import { useGroupProposals } from "@/hooks/use-resolution";
import type { ReconResolutionProposal } from "@/lib/types";

interface ResolutionGroupItemsProps {
  runId: string;
  groupKey: string;
  tickedAboveIds: string[];
  onTickAbove: (proposalId: string, ticked: boolean) => void;
  onInvestigate: (proposal: ReconResolutionProposal) => void;
}

export function ResolutionGroupItems({
  runId,
  groupKey,
  tickedAboveIds,
  onTickAbove,
  onInvestigate,
}: ResolutionGroupItemsProps) {
  const { data: proposals, isLoading } = useGroupProposals(runId, groupKey);
  if (isLoading) {
    return <p className="text-[13px] text-muted-foreground">Loading items…</p>;
  }
  if (!proposals?.length) {
    return <p className="text-[13px] text-muted-foreground">No items in this group.</p>;
  }
  const money = (p: ReconResolutionProposal) =>
    Number(p.proposed_amount).toLocaleString("en-US", {
      style: "currency",
      currency: p.currency || "USD",
    });
  return (
    <ul className="divide-y">
      {proposals.map((p) => (
        <li key={p.id} className="flex items-center justify-between gap-3 py-2">
          <div className="flex items-center gap-3">
            {p.above_materiality && p.status === "proposed" && (
              <input
                type="checkbox"
                checked={tickedAboveIds.includes(p.id)}
                onChange={(e) => onTickAbove(p.id, e.target.checked)}
                aria-label={`Include ${money(p)} item in approval`}
              />
            )}
            <div>
              <p className="text-[13px] text-foreground">{p.narrative}</p>
              <p className="text-xs text-muted-foreground">
                {money(p)}
                {p.above_materiality && (
                  <span className="ml-2 text-amber-700">above materiality</span>
                )}
                {p.status !== "proposed" && <span className="ml-2">· {p.status}</span>}
              </p>
            </div>
          </div>
          {p.action === "needs_human" && (
            <button
              type="button"
              onClick={() => onInvestigate(p)}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
            >
              <MessageSquare className="h-3.5 w-3.5" />
              Investigate in chat
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
```

`page.tsx` integration — the surgical rules (the flag-off path must be byte-identical in behavior):
1. Add imports: `ResolutionSummaryHeader`, `ResolutionGroupCard`, `ResolutionGroupItems`, `useResolutionSummary`, `useApproveResolutionGroup`, `useRejectResolutionGroup`.
2. Add state + hooks after the existing hook block:

```tsx
  const resolutionUiEnabled = useFeature("recon_resolution_ui");
  const resolutionSummary = useResolutionSummary(resolutionUiEnabled ? selectedRunId : null);
  const approveGroup = useApproveResolutionGroup(selectedRunId || "");
  const rejectGroup = useRejectResolutionGroup(selectedRunId || "");
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const [tickedAboveByGroup, setTickedAboveByGroup] = useState<Record<string, string[]>>({});
  const [groupResetSignal, setGroupResetSignal] = useState(0);
  const [showClassicView, setShowClassicView] = useState(false);
```

3. Wrap the existing "Tabs + results" JSX block (the `{!pipeline.isRunning && selectedRunId && (...)}` section) in `{!resolutionUiEnabled && ( <existing block untouched/> )}` — do the same for `ReconSummaryBar` and `ReconExceptionCard` sections.
4. Add the new surface as a sibling:

```tsx
      {resolutionUiEnabled && !pipeline.isRunning && selectedRunId && (
        <>
          <ResolutionSummaryHeader summary={resolutionSummary.data ?? null} />
          <div className="space-y-3">
            {resolutionSummary.data?.groups.map((group) => (
              <ResolutionGroupCard
                key={group.group_key}
                group={group}
                expanded={expandedGroup === group.group_key}
                onToggleExpand={() =>
                  setExpandedGroup(expandedGroup === group.group_key ? null : group.group_key)
                }
                isApproving={approveGroup.isPending}
                disabled={!reconEnabled || isRunClosed}
                includedAboveIds={tickedAboveByGroup[group.group_key] ?? []}
                resetSignal={groupResetSignal}
                onApprove={(notes, includedAboveIds) =>
                  approveGroup.mutate(
                    {
                      group_key: group.group_key,
                      notes: notes.trim() || undefined,
                      included_above_materiality_ids: includedAboveIds,
                    },
                    { onSuccess: () => setGroupResetSignal((n) => n + 1) }
                  )
                }
                onReject={() => rejectGroup.mutate({ group_key: group.group_key })}
              >
                <ResolutionGroupItems
                  runId={selectedRunId}
                  groupKey={group.group_key}
                  tickedAboveIds={tickedAboveByGroup[group.group_key] ?? []}
                  onTickAbove={(id, ticked) =>
                    setTickedAboveByGroup((prev) => {
                      const current = prev[group.group_key] ?? [];
                      return {
                        ...prev,
                        [group.group_key]: ticked
                          ? [...current, id]
                          : current.filter((x) => x !== id),
                      };
                    })
                  }
                  onInvestigate={(p) => handleInvestigate(p.result_id)}
                />
              </ResolutionGroupCard>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setShowClassicView((v) => !v)}
            className="text-[13px] text-muted-foreground underline-offset-2 hover:underline"
          >
            {showClassicView ? "Hide" : "Show"} all results (classic view)
          </button>
          {showClassicView && (
            /* render the SAME tab block JSX the flag-off branch uses (extract it
               to a local <ClassicBucketView/> function component inside page.tsx
               so both branches share one implementation) */
            <ClassicBucketView />
          )}
        </>
      )}
```

   `handleInvestigate` currently takes the shape used by `ReconResultsTable` rows — read its signature in `page.tsx` (~line 114) and adapt: it needs a result identifier to prefill the chat SuiteQL; pass `p.result_id` through whatever shape it expects. Extract the existing tabs+table+bulk-card JSX into a `function ClassicBucketView()` inside `page.tsx` (zero prop changes — it closes over the same state) and use it in both branches so there is exactly one copy.

- [ ] **Step 4: Run: `cd frontend && npx vitest run && npx tsc --noEmit` — expect PASS / no new type errors**

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/reconciliation/resolution-group-items.tsx frontend/src/app/\(dashboard\)/reconciliation/page.tsx frontend/src/components/reconciliation/__tests__/resolution-group-items.test.tsx
git commit -m "feat(recon-fe): summary-first surface behind recon_resolution_ui; classic view preserved"
```

---

### Task 12: Seeded e2e + full regression + lint

**Files:**
- Create: `backend/tests/test_resolution_flow_e2e.py`

- [ ] **Step 1: Write the e2e test** (run → plan → summary → approve fee group → carried_forward acknowledge → close readiness)

```python
"""Phase 1 e2e: seed → plan → summary → group approve → close readiness.

The T2 regression backbone for the summary-first rework. NetSuite is never
touched (Phase 1 is DB-only by design)."""

from decimal import Decimal

from sqlalchemy import select

from app.api.v1.reconciliation import (
    approve_resolution_group,
    get_close_readiness,
    get_resolution_summary,
    plan_resolutions,
)
from app.models.reconciliation import ReconciliationResult
from app.schemas.reconciliation import ResolutionGroupApprove
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def test_summary_first_flow_end_to_end(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")

    # $9 fee on a $1000 order = sub-materiality → one-click group-approvable.
    fee = await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="auto_classifications",
        match_type="deterministic", variance_type="fees",
        variance_amount=Decimal("9.00"), stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_fee", "order_reference": "R1"},
    )
    timing = await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="rules",
        match_type="fuzzy", variance_type="timing", variance_amount=Decimal("0"),
        stripe_amount=Decimal("50.00"), netsuite_amount=Decimal("50.00"),
        evidence={"charge_source_id": "ch_time", "order_reference": "R2"},
    )
    chargeback = await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="needs_review",
        match_type="deterministic", variance_type="chargeback",
        variance_amount=Decimal("42.00"), stripe_amount=Decimal("42.00"),
        netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_cb", "order_reference": "R3"},
    )
    await db.flush()

    # 1. plan
    plan = await plan_resolutions(str(run.id), user=user, db=db)
    assert plan["planned_count"] == 3

    # 2. summary-first payload
    summary = await get_resolution_summary(str(run.id), user=user, db=db)
    assert summary.proposals_count == 3
    assert summary.explained_count == 2  # chargeback stays needs_human

    # 3. approve fees, acknowledge timing
    await approve_resolution_group(
        str(run.id), "fees:book_fee_line:deposit",
        ResolutionGroupApprove(notes="e2e"), user=user, db=db,
    )
    await approve_resolution_group(
        str(run.id), "timing:carry_forward:none",
        ResolutionGroupApprove(), user=user, db=db,
    )

    for r in (fee, timing, chargeback):
        await db.refresh(r)
    assert fee.status == "approved"
    assert timing.status == "carried_forward"
    assert chargeback.status == "pending"  # untouched — needs human

    # 4. close readiness reflects the flow: only the chargeback blocks
    readiness = await get_close_readiness("2026-04", user=user, db=db)
    assert readiness.carried_forward == 1
    assert readiness.open_exceptions == 1  # the pending chargeback
```

- [ ] **Step 2: Run: `backend/.venv/bin/python -m pytest backend/tests/test_resolution_flow_e2e.py -v` — expect PASS**

- [ ] **Step 3: Full regression + lint**

Run: `backend/.venv/bin/python -m pytest backend/tests/ -x -q` (full backend suite)
Run: `cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: ALL PASS. Fix anything red before committing (max 3 self-heal attempts, then stop and report).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_resolution_flow_e2e.py
git commit -m "test(recon): Phase 1 e2e — plan → summary → group approve → close readiness"
```

---

## Post-plan gates (not tasks — release checklist)

1. Push branch to BOTH remotes; open PR (`origin`).
2. **T2 blocking gate:** `Workflow({name: "code-review-multiangle", args: {target: "<PR#>"}})` — non-empty `failed_angles` ⇒ re-run; check `codex_used`.
3. Supabase migration at merge: `.venv/bin/alembic upgrade head` (staging auto-migrates on deploy; verify).
4. Staging watch: any main merge auto-deploys; frontend deploy is manual (`./deploy-frontend.sh` with `NEXT_PUBLIC_BUILD_ID`).
5. Enable `recon_resolution_ui` for the uat-smoke tenant only; smoke the page; Framework enablement per spec Phase 4 criteria.

## Self-review notes (spec coverage)

- Spec Phase 1 bullet — migration ✅ (T1), planner ✅ (T3-4), summary/group endpoints ✅ (T6-7), page rework behind flag ✅ (T9-11), `carried_forward` + close-readiness ✅ (T7), evidence Proposals sheet ✅ (T8), cross-run guard ✅ (T4), audit conventions ✅ (T4, T7), e2e ✅ (T12).
- Deliberately NOT here (later phases): ResolutionAgent + chat tools (Phase 2); PostingService, `recon_posting` flag, `recon.post` permission, payload builders, period checks (Phase 3); live smoke + enablement (Phase 4).
