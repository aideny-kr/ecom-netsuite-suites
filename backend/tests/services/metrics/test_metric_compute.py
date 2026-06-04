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
