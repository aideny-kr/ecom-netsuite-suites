"""Guard that every NetSuite golden_dataset file has a partition_id declared
in frontmatter. Without this, ingest_domain_knowledge populates chunks with
partition_id=NULL and netsuite.yaml's rag_partitions return nothing.
"""

from pathlib import Path

import yaml

GOLDEN_DATASET_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "golden_dataset"

NETSUITE_PARTITION_MAP = {
    "suiteql-syntax-rules.md": "netsuite/suiteql-rules",
    "suiteql-example-queries.md": "netsuite/suiteql-rules",
    "common-errors-and-recovery.md": "netsuite/suiteql-rules",
    "date-and-time-patterns.md": "netsuite/suiteql-rules",
    "join-patterns-and-aggregation.md": "netsuite/joins",
    "transaction-relationships.md": "netsuite/joins",
    "transaction-types-and-statuses.md": "netsuite/transactions",
    "financial-statements.md": "netsuite/transactions",
    "multi-currency-rules.md": "netsuite/multi-currency",
    "record-types-and-columns.md": "netsuite/record-types",
    "custom-fields-and-records.md": "netsuite/record-types",
}


def _parse_frontmatter(content: str) -> dict:
    import re

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


class TestNetSuiteGoldenDatasetPartitions:
    def test_all_expected_files_exist(self):
        for filename in NETSUITE_PARTITION_MAP:
            path = GOLDEN_DATASET_DIR / filename
            assert path.is_file(), f"Missing golden dataset file: {path}"

    def test_all_files_declare_expected_partition(self):
        missing_or_wrong = []
        for filename, expected_partition in NETSUITE_PARTITION_MAP.items():
            path = GOLDEN_DATASET_DIR / filename
            content = path.read_text()
            fm = _parse_frontmatter(content)
            actual = fm.get("partition_id")
            if actual != expected_partition:
                missing_or_wrong.append(f"{filename}: expected {expected_partition!r}, got {actual!r}")
        assert not missing_or_wrong, "\n".join(missing_or_wrong)
