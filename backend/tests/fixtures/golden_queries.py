"""Helpers for loading and validating golden query test fixtures."""

import json
from pathlib import Path

from app.services.importance_classifier import ImportanceTier

_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "knowledge" / "golden_queries.json"

_REQUIRED_FIELDS = {
    "id",
    "tier",
    "category",
    "question",
    "sample_sql",
    "expected_sql_contains",
    "expected_sql_not_contains",
    "expected_tables",
}

_VALID_TIERS = {t.value for t in ImportanceTier}
_VALID_CATEGORIES = {t.name.lower() for t in ImportanceTier}


def load_golden_queries() -> list[dict]:
    """Load golden queries from JSON fixture."""
    with open(_FIXTURE_PATH) as f:
        return json.load(f)


def validate_schema(query: dict) -> None:
    """Validate a golden query has all required fields."""
    missing = _REQUIRED_FIELDS - set(query.keys())
    if missing:
        raise ValueError(f"Golden query {query.get('id', '?')} missing fields: {missing}")
    assert query["tier"] in _VALID_TIERS, f"Invalid tier: {query['tier']}"
    assert query["category"] in _VALID_CATEGORIES, f"Invalid category: {query['category']}"
    assert len(query["expected_sql_contains"]) > 0
    assert len(query["expected_tables"]) > 0
