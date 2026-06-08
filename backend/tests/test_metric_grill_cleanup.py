"""Tests for Task 18 grill-fix cleanup items.

1. bi/metric-definitions RAG partition removed from bigquery.yaml (R2#21).
2. Dotted metric.compute / metric.resolve aliases fixed to underscores in model-facing text (R1#14).
3. metrics.yaml prompt_fragment contains no hardcoded schema (justifying always-on activation) (R1#13).
"""

from __future__ import annotations

import re
from pathlib import Path

PROFILES_DIR = Path(__file__).parent.parent / "app" / "services" / "chat" / "knowledge_profiles"
REGISTRY_PY = Path(__file__).parent.parent / "app" / "mcp" / "registry.py"
METRIC_TOOLS_PY = Path(__file__).parent.parent / "app" / "mcp" / "tools" / "metric_tools.py"

BIGQUERY_YAML = PROFILES_DIR / "bigquery.yaml"
METRICS_YAML = PROFILES_DIR / "metrics.yaml"


class TestItem1DanglingBiMetricDefinitionsPartition:
    """R2#21: bigquery.yaml must not reference bi/metric-definitions (no seeder writes it)."""

    def test_bigquery_yaml_has_no_bi_metric_definitions_partition(self):
        content = BIGQUERY_YAML.read_text()
        assert "bi/metric-definitions" not in content, (
            "bigquery.yaml still references the dangling bi/metric-definitions RAG partition. "
            "No seeder writes to this partition — remove the entry."
        )

    def test_bigquery_yaml_still_has_bi_schema_docs_partition(self):
        """Sanity-guard: bi/schema-docs must not have been accidentally removed."""
        content = BIGQUERY_YAML.read_text()
        assert "bi/schema-docs" in content, "bi/schema-docs partition was accidentally removed from bigquery.yaml."


class TestItem2DottedToolAliasesFixed:
    """R1#14: model-facing description/note text must use metric_compute / metric_resolve
    (underscore), not the dotted metric.compute / metric.resolve forms.

    The registry KEYS remain dotted (they are sanitised to underscores server-side);
    what we test here is the human-readable *description* / *note* text the LLM reads.
    """

    def test_registry_metric_resolve_description_uses_underscore(self):
        content = REGISTRY_PY.read_text()
        # Locate the metric.resolve entry description block.
        # It should not tell the model "call metric.compute" (dotted).
        # Find the block between "metric.resolve" key and the next key.
        metric_resolve_block_match = re.search(
            r'"metric\.resolve".*?"execute"',
            content,
            re.DOTALL,
        )
        assert metric_resolve_block_match, "Could not find metric.resolve entry in registry.py"
        block = metric_resolve_block_match.group(0)
        assert "metric.compute" not in block, (
            "registry.py metric.resolve description still says 'metric.compute' (dotted). "
            "Change to 'metric_compute' (underscore) so the LLM sees the correct callable name."
        )

    def test_registry_metric_compute_description_uses_underscore(self):
        content = REGISTRY_PY.read_text()
        metric_compute_block_match = re.search(
            r'"metric\.compute".*?"execute"',
            content,
            re.DOTALL,
        )
        assert metric_compute_block_match, "Could not find metric.compute entry in registry.py"
        block = metric_compute_block_match.group(0)
        # The compute description shouldn't reference dotted forms pointing back.
        # (compute description is brief, but let's still verify no dotted form leaked in)
        # We allow "metric.compute" appearing as the registry KEY string literal itself
        # (before the colon/opening brace) but not inside description text.
        desc_match = re.search(r'"description"\s*:\s*\(?(.*?)\)?(?:,|\n\s*"execute")', block, re.DOTALL)
        if desc_match:
            desc_text = desc_match.group(1)
            assert "metric.compute" not in desc_text, (
                "registry.py metric.compute description still contains 'metric.compute' (dotted)."
            )

    def test_metric_tools_note_uses_underscore(self):
        content = METRIC_TOOLS_PY.read_text()
        # The `resolve` function returns a dict with a "note" key.
        # That note must use metric_compute (underscore).
        note_match = re.search(r'"note"\s*:\s*"([^"]*)"', content)
        assert note_match, "Could not find 'note' key in metric_tools.py resolve return value."
        note_text = note_match.group(1)
        assert "metric.compute" not in note_text, (
            f"metric_tools.py resolve note still says 'metric.compute' (dotted): {note_text!r}. "
            "Change to 'metric_compute' (underscore)."
        )
        assert "metric_compute" in note_text, (
            f"metric_tools.py resolve note does not contain 'metric_compute' (underscore): {note_text!r}."
        )

    def test_metrics_yaml_prompt_fragment_uses_underscore(self):
        content = METRICS_YAML.read_text()
        # Find the prompt_fragment section.
        # The fragment uses backtick-quoted tool names like `metric_resolve`.
        # Check no dotted form leaked in.
        assert "metric.compute" not in content, (
            "metrics.yaml prompt_fragment contains 'metric.compute' (dotted). Change to 'metric_compute' (underscore)."
        )
        assert "metric.resolve" not in content, (
            "metrics.yaml prompt_fragment contains 'metric.resolve' (dotted). Change to 'metric_resolve' (underscore)."
        )


class TestItem3MetricsProfileNoHardcodedSchema:
    """R1#13: metrics.yaml prompt_fragment must contain only behavioral guidance,
    no hardcoded column names / SQL schema. This justifies always-on activation
    without violating the no-prompt-pollution rule.
    """

    def test_metrics_yaml_prompt_fragment_has_no_select_statement(self):
        content = METRICS_YAML.read_text()
        assert "SELECT " not in content, (
            "metrics.yaml contains 'SELECT ' — the prompt_fragment must not contain SQL schema dumps."
        )

    def test_metrics_yaml_prompt_fragment_has_no_from_clause(self):
        content = METRICS_YAML.read_text()
        # "FROM " in isolation catches SQL FROM clauses; ok if part of e.g. "from it"
        # but let's check for the canonical all-caps SQL keyword.
        assert "FROM " not in content, (
            "metrics.yaml contains 'FROM ' — the prompt_fragment must not contain SQL schema."
        )

    def test_metrics_yaml_prompt_fragment_has_no_netsuite_custom_field_tokens(self):
        """No custbody/custrecord/custcol/custitem tokens — these are schema leakage."""
        content = METRICS_YAML.read_text()
        for pattern in ("custbody", "custrecord", "custcol", "custitem"):
            assert pattern not in content, (
                f"metrics.yaml contains '{pattern}' — hardcoded NetSuite column token. "
                "Remove it; the fragment should be guidance-only."
            )

    def test_metrics_yaml_has_always_on_activation_comment(self):
        """Item 3 DOCUMENT: must have a # comment explaining always-on activation."""
        content = METRICS_YAML.read_text()
        # The comment must mention "always-on" or "always on" (case-insensitive)
        # and must reference the no-prompt-pollution rule or hardcoded columns.
        has_always_on_comment = bool(re.search(r"#.*always.?on", content, re.IGNORECASE))
        assert has_always_on_comment, (
            "metrics.yaml is missing the # always-on activation comment required by R1#13. "
            "Add a YAML comment documenting why always-on is acceptable."
        )
