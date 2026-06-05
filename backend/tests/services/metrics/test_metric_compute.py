# backend/tests/services/metrics/test_metric_compute.py
import pytest

from app.services.metrics.metric_compute import ParamError, coerce_params, fill_query


def test_coerce_rejects_unknown_param():
    with pytest.raises(ParamError):
        coerce_params({"period_start": {"type": "date"}}, {"evil": "1 OR 1=1"})


def test_coerce_enum_rejects_out_of_set():
    with pytest.raises(ParamError):
        coerce_params({"region": {"type": "enum", "values": ["us", "eu"]}}, {"region": "'; DROP"})


def test_coerce_int_and_date():
    out = coerce_params(
        {"sub": {"type": "int"}, "period_start": {"type": "date"}},
        {"sub": "7", "period_start": "2026-01-01"},
    )
    assert out == {"sub": 7, "period_start": "2026-01-01"}


def test_fill_query_uses_coerced_literals():
    sql = fill_query("SELECT x WHERE sub=:sub AND d>=:period_start", {"sub": 7, "period_start": "2026-01-01"})
    assert sql == "SELECT x WHERE sub=7 AND d>='2026-01-01'"


def test_fill_query_rejects_residual_placeholder():
    with pytest.raises(ParamError):
        fill_query("SELECT x WHERE sub=:sub", {})


def test_fill_query_escapes_embedded_single_quote():
    """REAL injection invariant (F3, leg a). fill_query renders a string-typed coerced
    value inside a SQL string literal (`'<v>'`). If `v` itself contains a single quote,
    the naive `f"'{v}'"` breaks OUT of the literal, turning param data into SQL control.
    The classic payload `x' OR '1'='1` must be rendered as a SINGLE, structurally-inert
    string literal — every embedded quote doubled (`''`) per SQL escaping. The prior
    code did `f"'{v}'"` with no escaping, so the rendered SQL was
    `'x' OR '1'='1'` — three literals + boolean logic, an injection break-out."""
    payload = "x' OR '1'='1"
    out = fill_query("SELECT x WHERE region=:region", {"region": payload})
    # The whole value is one inert literal: every embedded ' is doubled to ''.
    assert out == "SELECT x WHERE region='x'' OR ''1''=''1'"
    # And there is no un-doubled quote that could close the literal early: stripping
    # the doubled-quote pairs leaves exactly the two outer delimiters.
    assert out.replace("''", "").count("'") == 2
