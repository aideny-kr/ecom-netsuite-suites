# backend/app/services/metrics/metric_authoring.py
"""Author-time validation for metric definitions (one-of, key-allowlist, DAG, params)."""

from app.services.metrics.expression_evaluator import ExpressionError, extract_dependencies

_SINGLE_SOURCE_KEYS = {"query", "dialect"}


class AuthoringError(ValueError):
    pass


def validate_definition(d: dict, *, allowed_cross_source_keys: set[str] | None = None) -> None:
    kind = d.get("source_kind")
    spec, expr = d.get("blessed_spec"), d.get("expression")

    if bool(spec) == bool(expr):
        raise AuthoringError("exactly one of blessed_spec / expression must be set")

    if kind == "expression":
        if not expr or not d.get("depends_on"):
            raise AuthoringError("expression metrics need expression + depends_on")
        try:
            deps = set(extract_dependencies(expr))
        except ExpressionError as ex:
            raise AuthoringError(str(ex)) from ex
        if d["key"] in deps:
            raise AuthoringError("expression cannot reference itself (cycle)")
        if deps != set(d["depends_on"]):
            raise AuthoringError("depends_on must match expression references")
    else:
        if not isinstance(spec, dict):
            raise AuthoringError("query-backed metric needs a blessed_spec object")
        allowed = _SINGLE_SOURCE_KEYS if kind in ("suiteql", "bigquery") else (allowed_cross_source_keys or set())
        unknown = set(spec) - allowed
        if unknown:
            raise AuthoringError(f"blessed_spec has keys not in the live tool schema: {sorted(unknown)}")
