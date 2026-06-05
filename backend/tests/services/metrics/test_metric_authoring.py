# backend/tests/services/metrics/test_metric_authoring.py
import pytest

from app.services.metrics.metric_authoring import AuthoringError, validate_definition

_CROSS_SOURCE_KEYS = {
    "left_query",
    "left_dialect",
    "right_query",
    "right_dialect",
    "join_keys",
    "join_type",
    "select",
    "pivot",
}


def test_rejects_unknown_cross_source_key():
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "x",
                "source_kind": "cross_source",
                "blessed_spec": {"left_query": "a", "right_query": "b", "join_keys": ["id"], "aggregations": "sum"},
            },
            allowed_cross_source_keys=_CROSS_SOURCE_KEYS,
        )


def test_rejects_expression_cycle():
    with pytest.raises(AuthoringError):
        validate_definition({"key": "a", "source_kind": "expression", "expression": "a / b", "depends_on": ["a", "b"]})


def test_rejects_both_spec_and_expression():
    with pytest.raises(AuthoringError):
        validate_definition(
            {"key": "x", "source_kind": "suiteql", "blessed_spec": {"query": "SELECT 1"}, "expression": "a/b"}
        )


def test_accepts_valid_expression():
    validate_definition(
        {
            "key": "net_margin",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        }
    )


# ── (b) params_schema type allowlist + :param binding ──────────────────────────


def test_rejects_param_type_not_in_allowlist():
    """REAL invariant: a free-text 'string' param type lets unconstrained text flow
    into the filled SQL (the §6 binding hole). validate_definition MUST reject any
    param type outside {date,int,enum,period}. The prior code never inspected
    params_schema at all, so this string param sailed through to compute."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "gross_revenue",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE name=:name", "dialect": "suiteql"},
                "params_schema": {"name": {"type": "string"}},
            }
        )


def test_rejects_enum_without_values():
    """An enum param with no (or empty) values list is an open hole — coerce_params
    would reject every value at runtime, but author-time must catch the malformed
    declaration so a blessed metric is never persisted un-runnable."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "rev_by_region",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE region=:region", "dialect": "suiteql"},
                "params_schema": {"region": {"type": "enum", "values": []}},
            }
        )


def test_rejects_query_placeholder_not_declared_in_params_schema():
    """Every :name in the blessed query MUST be declared in params_schema, else
    fill_query would leave a residual placeholder (or worse, an undeclared param
    bypasses type coercion). The prior code never cross-checked the two."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "gross_revenue",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE sub=:subsidiary", "dialect": "suiteql"},
                "params_schema": {"period": {"type": "period"}},  # :subsidiary undeclared
            }
        )


def test_rejects_declared_param_absent_from_query():
    """And vice-versa: a declared non-period param that never appears as a :name in
    the query is dead config that silently never binds — reject it at author-time.
    (period is exempt: it expands to :period_start/:period_end, not a literal :period.)"""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "gross_revenue",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE d>=:period_start AND d<=:period_end", "dialect": "suiteql"},
                "params_schema": {
                    "period": {"type": "period"},
                    "subsidiary": {"type": "int"},  # declared but never referenced
                },
            }
        )


def test_accepts_period_expanding_to_start_end_placeholders():
    """A period param legitimately drives :period_start/:period_end placeholders
    (coerce_params expands it). This well-formed query-backed metric must PASS —
    guards against the binding check being too strict and rejecting valid period use."""
    validate_definition(
        {
            "key": "gross_revenue",
            "source_kind": "suiteql",
            "blessed_spec": {
                "query": "SELECT SUM(amount) FROM transactionline WHERE trandate>=:period_start AND trandate<=:period_end",
                "dialect": "suiteql",
            },
            "params_schema": {"period": {"type": "period"}},
        }
    )


def test_accepts_int_and_enum_params_bound_in_query():
    """Well-formed int + enum params, each referenced as a :name in the query and
    each carrying a valid type (enum with non-empty values) → PASS."""
    validate_definition(
        {
            "key": "rev_by_sub_region",
            "source_kind": "suiteql",
            "blessed_spec": {
                "query": "SELECT SUM(amount) FROM transactionline WHERE subsidiary=:sub AND region=:region",
                "dialect": "suiteql",
            },
            "params_schema": {
                "sub": {"type": "int"},
                "region": {"type": "enum", "values": ["us", "eu"]},
            },
        }
    )
