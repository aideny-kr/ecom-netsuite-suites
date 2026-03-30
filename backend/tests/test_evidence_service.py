"""Tests for evidence pack generation."""

import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app.services.reconciliation.evidence_service import EvidencePackGenerator


@pytest.fixture
def sample_results() -> list[dict]:
    """Sample reconciliation results for evidence generation."""
    return [
        {
            "id": str(uuid.uuid4()),
            "match_type": "deterministic",
            "confidence": Decimal("1.0"),
            "status": "auto_matched",
            "stripe_amount": Decimal("1455.00"),
            "netsuite_amount": Decimal("1455.00"),
            "variance_amount": Decimal("0.00"),
            "variance_type": None,
            "variance_explanation": None,
            "currency": "USD",
            "match_rule": "exact_payout_id",
            "evidence": {
                "payout_source_id": "po_test001",
                "deposit_ids": ["12001"],
            },
        },
        {
            "id": str(uuid.uuid4()),
            "match_type": "fuzzy",
            "confidence": Decimal("0.85"),
            "status": "suggested",
            "stripe_amount": Decimal("3104.00"),
            "netsuite_amount": Decimal("3104.00"),
            "variance_amount": Decimal("0.00"),
            "variance_type": "timing",
            "variance_explanation": "Deposit is 2 days after payout arrival",
            "currency": "USD",
            "match_rule": "amount_exact+within_2_days",
            "evidence": {
                "payout_source_id": "po_test005",
                "deposit_ids": ["12005"],
            },
        },
        {
            "id": str(uuid.uuid4()),
            "match_type": "unmatched",
            "confidence": Decimal("0.0"),
            "status": "pending",
            "stripe_amount": Decimal("863.30"),
            "netsuite_amount": None,
            "variance_amount": Decimal("863.30"),
            "variance_type": "missing",
            "variance_explanation": "No matching deposit found",
            "currency": "USD",
            "match_rule": "no_match",
            "evidence": {
                "payout_source_id": "po_test007",
                "deposit_ids": [],
            },
        },
    ]


class TestEvidencePackGenerator:
    def test_generates_excel(self, sample_results):
        """Evidence pack should produce a valid Excel file."""
        generator = EvidencePackGenerator()
        excel_bytes = generator.generate_excel(
            results=sample_results,
            run_id="test-run-001",
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )

        assert isinstance(excel_bytes, io.BytesIO)
        wb = load_workbook(excel_bytes)
        assert len(wb.sheetnames) >= 2  # Summary + Details at minimum

    def test_summary_sheet_has_totals(self, sample_results):
        """Summary sheet should contain match/exception/unmatched counts."""
        generator = EvidencePackGenerator()
        excel_bytes = generator.generate_excel(
            results=sample_results,
            run_id="test-run-002",
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )

        wb = load_workbook(excel_bytes)
        summary_ws = wb["Summary"]

        # Check that summary data exists (look for key labels)
        all_values = [cell.value for row in summary_ws.iter_rows() for cell in row if cell.value]
        text = " ".join(str(v) for v in all_values)

        assert "Matched" in text or "matched" in text
        assert "Exception" in text or "exception" in text or "Unmatched" in text

    def test_exceptions_sheet_exists(self, sample_results):
        """Should have an Exceptions sheet with unmatched/low-confidence items."""
        generator = EvidencePackGenerator()
        excel_bytes = generator.generate_excel(
            results=sample_results,
            run_id="test-run-003",
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )

        wb = load_workbook(excel_bytes)
        assert "Exceptions" in wb.sheetnames

    def test_empty_results_still_generates(self):
        """Even with no results, should produce a valid Excel."""
        generator = EvidencePackGenerator()
        excel_bytes = generator.generate_excel(
            results=[],
            run_id="test-run-empty",
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )

        assert isinstance(excel_bytes, io.BytesIO)
        wb = load_workbook(excel_bytes)
        assert "Summary" in wb.sheetnames
