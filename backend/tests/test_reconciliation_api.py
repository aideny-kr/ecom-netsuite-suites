"""Tests for reconciliation API endpoints."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal


class TestReconAPI:
    """Verify schema/route structure (not full integration — that requires DB)."""

    def test_recon_run_create_schema(self):
        from app.schemas.reconciliation import ReconRunCreate

        req = ReconRunCreate(
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )
        assert req.date_from == date(2026, 3, 1)
        assert req.subsidiary_id is None
        assert req.payout_ids is None

    def test_recon_run_create_with_subsidiary(self):
        from app.schemas.reconciliation import ReconRunCreate

        req = ReconRunCreate(
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
            subsidiary_id="sub_001",
        )
        assert req.subsidiary_id == "sub_001"

    def test_recon_result_response_schema(self):
        from app.schemas.reconciliation import ReconResultResponse

        resp = ReconResultResponse(
            id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            payout_id=None,
            deposit_id=None,
            match_type="deterministic",
            confidence=Decimal("1.0"),
            status="auto_matched",
            stripe_amount=Decimal("1000.00"),
            netsuite_amount=Decimal("1000.00"),
            variance_amount=Decimal("0"),
            variance_type=None,
            variance_explanation=None,
            currency="USD",
            match_rule="exact_payout_id",
            created_at=datetime.now(timezone.utc),
        )
        assert resp.match_type == "deterministic"

    def test_recon_run_response_schema(self):
        from app.schemas.reconciliation import ReconRunResponse

        resp = ReconRunResponse(
            id=str(uuid.uuid4()),
            tenant_id=str(uuid.uuid4()),
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
            subsidiary_id=None,
            status="completed",
            total_payouts=100,
            total_deposits=95,
            matched_count=90,
            exception_count=5,
            unmatched_count=10,
            total_variance=Decimal("432.50"),
            created_at=datetime.now(timezone.utc),
        )
        assert resp.matched_count == 90

    def test_recon_approve_schema(self):
        from app.schemas.reconciliation import ReconResultApprove

        req = ReconResultApprove(result_id=str(uuid.uuid4()))
        assert req.notes is None

    def test_recon_run_summary_match_rate(self):
        from app.schemas.reconciliation import ReconRunSummary

        summary = ReconRunSummary(
            run_id=str(uuid.uuid4()),
            status="completed",
            total_payouts=100,
            total_deposits=95,
            matched_count=90,
            exception_count=5,
            unmatched_count=10,
            total_variance=Decimal("432.50"),
            match_rate=Decimal("90.0"),
        )
        assert summary.match_rate == Decimal("90.0")

    def test_recon_run_create_with_payout_ids(self):
        from app.schemas.reconciliation import ReconRunCreate

        req = ReconRunCreate(
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
            payout_ids=["po_abc", "po_def"],
        )
        assert req.payout_ids == ["po_abc", "po_def"]

    def test_router_registered(self):
        from app.api.v1.reconciliation import router

        routes = {route.path for route in router.routes}
        assert "/reconciliation/runs" in routes
        assert "/reconciliation/runs/{run_id}" in routes
        assert "/reconciliation/runs/{run_id}/results" in routes
        assert "/reconciliation/results/{result_id}/approve" in routes
        assert "/reconciliation/evidence/{run_id}" in routes
        assert "/reconciliation/close/{period}" in routes
