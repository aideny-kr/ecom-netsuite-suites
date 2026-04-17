"""Regression tests for SuiteQL dialect rules embedded in the NetSuite
knowledge profile.

These tests guard against rule loss during profile edits. Each rule is
here because its absence caused a documented production failure. Do not
delete tests when "cleaning up" — read the docstring of the failing test
for the incident context.

Phase 2 (2026-04-16, PR A): rules moved from unified_agent._SYSTEM_PROMPT
into knowledge_profiles/netsuite.yaml's prompt_fragment. Assertions
below now read the profile's prompt_fragment via the loader.
"""

import pytest

from app.services.chat.knowledge_profiles.loader import load_all_profiles


@pytest.fixture(scope="module")
def netsuite_prompt_fragment() -> str:
    """Load the netsuite profile's prompt_fragment string once for the module."""
    profiles = load_all_profiles()
    netsuite = next((p for p in profiles if p.profile_id == "netsuite"), None)
    assert netsuite is not None, "netsuite.yaml did not load; check profile file syntax"
    return netsuite.prompt_fragment


class TestAddressTablesRule:
    """Verify the ADDRESS TABLES block teaches the canonical shipping/billing
    address join pattern.

    Incident: 2026-04-16 staging session burned 10+ tool calls rediscovering
    `transactionShippingAddress` join key. Agent kept trying `sa.recordOwner = t.id`
    and `sa.transaction = t.id` (both wrong). Correct join is `sa.nKey = t.shippingAddress`.
    See spec docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md.
    """

    def test_block_header_present(self, netsuite_prompt_fragment):
        assert "ADDRESS TABLES" in netsuite_prompt_fragment

    def test_correct_shipping_join_taught(self, netsuite_prompt_fragment):
        assert "transactionShippingAddress" in netsuite_prompt_fragment
        assert "sa.nKey = t.shippingAddress" in netsuite_prompt_fragment

    def test_correct_billing_join_taught(self, netsuite_prompt_fragment):
        assert "transactionBillingAddress" in netsuite_prompt_fragment
        assert "ba.nKey = t.billingAddress" in netsuite_prompt_fragment

    def test_wrong_join_keys_explicitly_warned(self, netsuite_prompt_fragment):
        # The three wrong keys the agent kept trying in the 2026-04-16 staging session
        assert "sa.recordOwner = t.id" in netsuite_prompt_fragment
        assert "sa.transaction = t.id" in netsuite_prompt_fragment
        assert "sa.id = t.shippingAddress" in netsuite_prompt_fragment

    def test_country_resolution_taught(self, netsuite_prompt_fragment):
        # Both readable display name and raw ISO code are valid
        assert "BUILTIN.DF(sa.country)" in netsuite_prompt_fragment

    def test_custbody_country_warning_present(self, netsuite_prompt_fragment):
        # The custbody fallback was the agent's wrong escape hatch
        assert "custbody" in netsuite_prompt_fragment.lower()
        # Locate the warning specifically in the address-tables area
        block_start = netsuite_prompt_fragment.find("ADDRESS TABLES")
        block_end = netsuite_prompt_fragment.find("TRANSACTION NUMBER CONVENTIONS", block_start)
        block = netsuite_prompt_fragment[block_start:block_end]
        assert "custom body" in block.lower() or "custbody" in block.lower()

    def test_block_placed_after_custom_list_fields(self, netsuite_prompt_fragment):
        # Locality matters: the block sits with related field-resolution rules
        custom_list_pos = netsuite_prompt_fragment.find("CUSTOM LIST FIELDS")
        address_pos = netsuite_prompt_fragment.find("ADDRESS TABLES")
        txn_num_pos = netsuite_prompt_fragment.find("TRANSACTION NUMBER CONVENTIONS")
        assert custom_list_pos < address_pos < txn_num_pos
