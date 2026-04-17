"""Guard that the canonical shipping-country worked example stays in the
golden_dataset. This pattern is referenced by PR #45 (Phase 1) and shipped
to RAG via Phase 2 ingest; losing it would reopen the April-16 regression.
"""

from pathlib import Path

GOLDEN_DATASET_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "golden_dataset"
JOIN_PATTERNS_FILE = GOLDEN_DATASET_DIR / "join-patterns-and-aggregation.md"


class TestShippingCountryWorkedExample:
    def test_file_exists(self):
        assert JOIN_PATTERNS_FILE.is_file(), f"Golden dataset file missing: {JOIN_PATTERNS_FILE}"

    def test_section_header_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        assert "## Worked Example: Sales by Shipping Country" in content

    def test_canonical_join_key_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        assert "sa.nKey = t.shippingAddress" in content

    def test_builtin_df_country_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        assert "BUILTIN.DF(sa.country)" in content

    def test_revenue_filters_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        # The standard transactionline revenue filters
        assert "tl.mainline = 'F'" in content
        assert "tl.taxline = 'F'" in content
        assert "tl.assemblycomponent = 'F'" in content

    def test_wrong_join_keys_warned(self):
        content = JOIN_PATTERNS_FILE.read_text()
        # The three wrong keys the agent kept trying
        assert "sa.recordOwner = t.id" in content or "NOT" in content
        assert "custbody" in content.lower()

    def test_example_is_at_end_of_file(self):
        """Append-at-end preserves prior chunks' chunk_index on re-ingest."""
        content = JOIN_PATTERNS_FILE.read_text()
        header_pos = content.find("## Worked Example: Sales by Shipping Country")
        assert header_pos > 0, "Worked example section not found"
        remaining_after = content[header_pos:]
        # The section should be at the tail — no more H2 headings after it
        subsequent_h2 = remaining_after.find("\n## ", 1)
        assert subsequent_h2 == -1, (
            f"Another H2 heading appears after the worked example (offset {subsequent_h2}); "
            f"the worked example must be the LAST section to preserve chunk_index on re-ingest."
        )
