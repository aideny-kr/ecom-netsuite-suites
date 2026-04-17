"""Regression tests for SuiteQL dialect rules embedded in the unified agent prompt.

These tests guard against rule loss during prompt edits. Each rule is here because
its absence caused a documented production failure. Do not delete tests when
"cleaning up" — read the docstring of the failing test for the incident context.
"""

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT


class TestAddressTablesRule:
    """Verify the ADDRESS TABLES block teaches the canonical shipping/billing
    address join pattern.

    Incident: 2026-04-16 staging session burned 10+ tool calls rediscovering
    `transactionShippingAddress` join key. Agent kept trying `sa.recordOwner = t.id`
    and `sa.transaction = t.id` (both wrong). Correct join is `sa.nKey = t.shippingAddress`.
    See spec docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md.
    """

    def test_block_header_present(self):
        assert "ADDRESS TABLES" in _SYSTEM_PROMPT

    def test_correct_shipping_join_taught(self):
        assert "transactionShippingAddress" in _SYSTEM_PROMPT
        assert "sa.nKey = t.shippingAddress" in _SYSTEM_PROMPT

    def test_correct_billing_join_taught(self):
        assert "transactionBillingAddress" in _SYSTEM_PROMPT
        assert "ba.nKey = t.billingAddress" in _SYSTEM_PROMPT

    def test_wrong_join_keys_explicitly_warned(self):
        # The three wrong keys the agent kept trying in the 2026-04-16 staging session
        assert "sa.recordOwner = t.id" in _SYSTEM_PROMPT
        assert "sa.transaction = t.id" in _SYSTEM_PROMPT
        assert "sa.id = t.shippingAddress" in _SYSTEM_PROMPT

    def test_country_resolution_taught(self):
        # Both readable display name and raw ISO code are valid
        assert "BUILTIN.DF(sa.country)" in _SYSTEM_PROMPT

    def test_custbody_country_warning_present(self):
        # The custbody fallback was the agent's wrong escape hatch
        assert "custbody" in _SYSTEM_PROMPT.lower()
        # Locate the warning specifically in the address-tables area
        block_start = _SYSTEM_PROMPT.find("ADDRESS TABLES")
        block_end = _SYSTEM_PROMPT.find("TRANSACTION NUMBER CONVENTIONS", block_start)
        block = _SYSTEM_PROMPT[block_start:block_end]
        assert "custom body" in block.lower() or "custbody" in block.lower()

    def test_block_placed_after_custom_list_fields(self):
        # Locality matters: the block sits with related field-resolution rules
        custom_list_pos = _SYSTEM_PROMPT.find("CUSTOM LIST FIELDS")
        address_pos = _SYSTEM_PROMPT.find("ADDRESS TABLES")
        txn_num_pos = _SYSTEM_PROMPT.find("TRANSACTION NUMBER CONVENTIONS")
        assert custom_list_pos < address_pos < txn_num_pos
