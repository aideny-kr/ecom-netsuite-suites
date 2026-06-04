"""Tests for the shared, tenant-configurable order-reference extraction module.

R3 Part 1 (de-Framework the order key). Two layers:

  1. Pure-python ``extract_order_ref`` behavior — default pattern reproduces every
     current ``R\\d{9}`` case byte-identically (the #1 invariant for Framework),
     a custom pattern extracts correctly, a malformed pattern falls back to the
     default + logs (never raises), and group(1)-vs-group(0) handling. These run
     in the implementer environment (no DB).
  2. DB-backed ``load_order_ref_pattern`` — a tenant with a set pattern returns it,
     a NULL/no-config tenant returns the default. Written rigorously following the
     existing recon DB-test patterns but NOT run here (no DB); the PM runs it
     post-flight against the local docker Postgres via the conftest ``db`` fixture.
"""

from __future__ import annotations

import pytest

from app.services.reconciliation.order_ref import (
    DEFAULT_ORDER_REF_PATTERN,
    extract_order_ref,
    load_order_ref_pattern,
)

# ---------------------------------------------------------------------------
# 1. Default pattern — must reproduce ALL current R\d{9} cases byte-identically
#    (behavior-preserving for Framework; #1 invariant).
# ---------------------------------------------------------------------------


class TestDefaultPattern:
    def test_default_constant(self):
        """The default is exactly the prior hardcoded pattern (one capture group)."""
        assert DEFAULT_ORDER_REF_PATTERN == r"(R\d{9})"

    def test_stripe_marketplace_order(self):
        assert extract_order_ref("Framework Marketplace Order ID: R628489275-XU9EPZPD") == "R628489275"

    def test_stripe_with_different_suffix(self):
        assert extract_order_ref("Framework Marketplace Order ID: R234917689-UZQLQUEA") == "R234917689"

    def test_netsuite_sales_order(self):
        assert extract_order_ref("Sales Order #R577684612") == "R577684612"

    def test_bare_order_number(self):
        assert extract_order_ref("R123456789") == "R123456789"

    def test_from_longer_string(self):
        assert extract_order_ref("SO created from R123456789 via import") == "R123456789"

    def test_uses_search_not_match(self):
        """The ref need not be anchored at the start — search, not match."""
        assert extract_order_ref("prefix text then R987654321 trailing") == "R987654321"

    def test_no_match(self):
        assert extract_order_ref("STRIPE PAYOUT") is None

    def test_no_match_plain_number(self):
        assert extract_order_ref("Sales Order #12345") is None

    def test_rejects_eight_digits(self):
        """R followed by fewer than 9 digits must NOT match."""
        assert extract_order_ref("R12345678") is None

    def test_multiple_refs_returns_first(self):
        assert extract_order_ref("R111111111 and R222222222") == "R111111111"

    def test_none_input(self):
        assert extract_order_ref(None) is None

    def test_empty_string(self):
        assert extract_order_ref("") is None

    def test_explicit_none_pattern_is_default(self):
        """pattern=None is the sentinel for the engine default."""
        assert extract_order_ref("Sales Order #R577684612", None) == "R577684612"


# ---------------------------------------------------------------------------
# 2. Custom patterns — a non-default tenant pattern extracts correctly.
# ---------------------------------------------------------------------------


class TestCustomPattern:
    def test_custom_hash_digits_pattern(self):
        """A custom (#\\d{4,}) pattern extracts via group(1)."""
        assert extract_order_ref("Order #12345 placed", pattern=r"(#\d{4,})") == "#12345"

    def test_custom_ten_digit_pattern(self):
        """A custom (\\d{10}) pattern extracts exactly 10 digits via group(1)."""
        assert extract_order_ref("ref 1234567890 end", pattern=r"(\d{10})") == "1234567890"

    def test_custom_pattern_rejects_when_no_match(self):
        assert extract_order_ref("R628489275", pattern=r"(#\d{4,})") is None

    def test_custom_pattern_differs_from_default(self):
        """The same description extracts differently under a custom vs default pattern."""
        text = "Sales Order #12345 ref R577684612"
        assert extract_order_ref(text) == "R577684612"
        assert extract_order_ref(text, pattern=r"(#\d{4,})") == "#12345"

    def test_group0_when_no_capture_group(self):
        """A pattern with NO capture group returns the whole match (group(0))."""
        assert extract_order_ref("Order R577684612 here", pattern=r"R\d{9}") == "R577684612"

    def test_group1_when_capture_group(self):
        """A pattern WITH a capture group returns group(1), not the full match."""
        # The full match is "#12345" but group(1) is the digits only.
        assert extract_order_ref("Order #12345 here", pattern=r"#(\d{4,})") == "12345"


# ---------------------------------------------------------------------------
# 3. Malformed pattern — fall back to the default + log, NEVER raise.
# ---------------------------------------------------------------------------


class TestMalformedPattern:
    def test_unbalanced_paren_falls_back_to_default(self):
        """A malformed regex '(' must not raise; it falls back to the default."""
        assert extract_order_ref("Sales Order #R577684612", pattern="(") == "R577684612"

    def test_malformed_does_not_raise(self):
        """Extraction must never raise on a bad pattern, even for non-matching text."""
        # Should not raise; returns None (default pattern doesn't match this text).
        assert extract_order_ref("no ref here", pattern="(") is None

    def test_malformed_logs_warning(self):
        """A malformed pattern emits a structlog warning naming the bad pattern.

        Uses a pattern unique to this test so the ``lru_cache`` on ``_compiled``
        is guaranteed to miss (and therefore log) rather than serve a cached
        compile from another test.
        """
        from structlog.testing import capture_logs

        bad_pattern = "(?P<unterminated"  # unique malformed regex => cache miss here
        with capture_logs() as logs:
            result = extract_order_ref("Sales Order #R577684612", pattern=bad_pattern)

        # Fell back to the default pattern (still extracts R\d{9}).
        assert result == "R577684612"
        # A warning was emitted about the malformed pattern fallback.
        warnings = [
            e for e in logs if e.get("log_level") == "warning" and e.get("event") == "order_ref.invalid_pattern"
        ]
        assert warnings, f"expected an order_ref.invalid_pattern warning, got: {logs}"
        assert warnings[0]["pattern"] == bad_pattern

    def test_unterminated_char_class_falls_back(self):
        assert extract_order_ref("Sales Order #R577684612", pattern="[a-") == "R577684612"

    def test_non_string_pattern_falls_back_to_default(self):
        """A non-string pattern (e.g. an int leaked from a bad TenantConfig) must
        not raise TypeError; it falls back to the default R\\d{9} pattern."""
        # 123 would raise TypeError inside re.compile if uncaught.
        assert extract_order_ref("Sales Order #R577684612", pattern=123) == "R577684612"

    def test_non_string_pattern_does_not_raise(self):
        """A non-string pattern never raises, even when the default doesn't match."""
        assert extract_order_ref("no ref here", pattern=123) is None

    def test_non_string_pattern_logs_warning(self):
        """A non-string pattern emits the same order_ref.invalid_pattern warning."""
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            result = extract_order_ref("Sales Order #R577684612", pattern=456789)

        assert result == "R577684612"
        warnings = [
            e for e in logs if e.get("log_level") == "warning" and e.get("event") == "order_ref.invalid_pattern"
        ]
        assert warnings, f"expected an order_ref.invalid_pattern warning, got: {logs}"
        assert warnings[0]["pattern"] == 456789

    def test_pathological_pattern_falls_back_to_default(self):
        """A syntactically valid but pathological pattern (huge repeat count) raises
        OverflowError inside re.compile; it must fall back to the default, not crash
        the recon/deposit run."""
        assert extract_order_ref("Sales Order #R577684612", pattern="a{99999999999}") == "R577684612"

    def test_pathological_pattern_does_not_raise(self):
        """A pathological pattern never raises, even when the default doesn't match."""
        assert extract_order_ref("no ref here", pattern="a{99999999999}") is None


# ---------------------------------------------------------------------------
# 4. DB-backed load_order_ref_pattern (NOT run here — PM runs post-flight).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLoadOrderRefPattern:
    async def test_set_pattern_returned(self, db, tenant_a):
        """A tenant with a non-NULL order_ref_pattern returns that pattern."""
        from sqlalchemy import select

        from app.models.tenant import TenantConfig

        cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
        cfg.order_ref_pattern = r"(#\d{4,})"
        await db.flush()

        pattern = await load_order_ref_pattern(db, str(tenant_a.id))
        assert pattern == r"(#\d{4,})"

    async def test_null_pattern_returns_default(self, db, tenant_a):
        """A tenant whose order_ref_pattern is NULL falls back to the engine default."""
        # tenant_a's config is created without order_ref_pattern -> NULL.
        pattern = await load_order_ref_pattern(db, str(tenant_a.id))
        assert pattern == DEFAULT_ORDER_REF_PATTERN

    async def test_no_config_returns_default(self, db):
        """A tenant with no TenantConfig row falls back to the engine default."""
        from app.models.tenant import Tenant

        tenant = Tenant(name="No Config", slug=f"no-config-{__import__('uuid').uuid4().hex[:8]}", is_active=True)
        db.add(tenant)
        await db.flush()

        pattern = await load_order_ref_pattern(db, str(tenant.id))
        assert pattern == DEFAULT_ORDER_REF_PATTERN
