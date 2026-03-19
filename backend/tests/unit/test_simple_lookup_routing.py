"""TDD: Route simple lookups to Haiku for 10x faster, 10x cheaper responses.

Simple queries like "show me SO-12345" or "how many open POs" don't need
Sonnet. Route them to Haiku when importance_tier is CASUAL.
"""

import pytest

from app.services.chat.orchestrator import _is_simple_lookup


class TestIsSimpleLookup:
    # ── Should match (True) ──
    def test_transaction_by_number(self):
        assert _is_simple_lookup("show me SO-12345") is True

    def test_item_by_sku(self):
        assert _is_simple_lookup("look up item FRANCR000B") is True

    def test_rma_by_number(self):
        assert _is_simple_lookup("what's the status of RMA 789") is True

    def test_po_by_number(self):
        assert _is_simple_lookup("PO12345") is True

    def test_simple_count(self):
        assert _is_simple_lookup("how many open sales orders") is True

    def test_invoice_lookup(self):
        assert _is_simple_lookup("find invoice INV-9876") is True

    def test_customer_lookup(self):
        assert _is_simple_lookup("look up customer Acme Corp") is True

    # ── Should NOT match (False) ──
    def test_comparison(self):
        assert _is_simple_lookup("compare Q1 vs Q2 revenue") is False

    def test_complex_analysis(self):
        assert _is_simple_lookup("show me all RMAs from Panurgy with their received dates") is False

    def test_trend(self):
        assert _is_simple_lookup("month over month revenue trend") is False

    def test_breakdown(self):
        assert _is_simple_lookup("sales breakdown by platform") is False

    def test_why_question(self):
        assert _is_simple_lookup("why is our margin declining") is False

    def test_multi_entity(self):
        assert _is_simple_lookup("show me all invoices with their payments and credits") is False

    def test_yoy(self):
        assert _is_simple_lookup("sales by class FY2025 vs FY2026") is False

    def test_documentation(self):
        assert _is_simple_lookup("how do I create a saved search") is False

    def test_script_request(self):
        assert _is_simple_lookup("write a suitelet for file uploads") is False


class TestHaikuModelRouting:
    def test_haiku_model_constant_exists(self):
        from app.services.chat.orchestrator import HAIKU_MODEL
        assert "haiku" in HAIKU_MODEL.lower()
