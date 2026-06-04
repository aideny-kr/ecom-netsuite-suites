"""Tests for the deterministic cross-source join engine."""

import pytest

from app.services.join_service import join_rows

LEFT = {
    "columns": ["sku", "ns_sales"],
    "rows": [["A-1", "100"], ["A-2", "200"], ["A-3", "300"]],
}
RIGHT = {
    "columns": ["item", "bq_spend"],
    "rows": [["A-1", "10"], ["A-2", "20"], ["A-9", "90"]],
}


def test_duckdb_importable():
    import duckdb

    con = duckdb.connect(database=":memory:")
    try:
        assert con.execute("SELECT 42").fetchone()[0] == 42
    finally:
        con.close()


def test_inner_join_single_key():
    out = join_rows(LEFT, RIGHT, [{"left": "sku", "right": "item"}], "inner")
    assert out["columns"] == ["sku", "ns_sales", "bq_spend"]  # right join key dropped
    assert out["row_count"] == 2
    rows = sorted(out["rows"])
    assert rows == [["A-1", "100", "10"], ["A-2", "200", "20"]]
    assert out["joined"] is True


def test_left_join_keeps_unmatched_left():
    out = join_rows(LEFT, RIGHT, [{"left": "sku", "right": "item"}], "left")
    assert out["row_count"] == 3
    by_sku = {r[0]: r for r in out["rows"]}
    assert by_sku["A-3"] == ["A-3", "300", None]  # no right match


def test_numeric_key_coercion():
    left = {"columns": ["id", "x"], "rows": [["123", "a"]]}
    right = {"columns": ["id", "y"], "rows": [["123.0", "b"]]}  # numeric-string mismatch
    out = join_rows(left, right, [{"left": "id", "right": "id"}], "inner")
    assert out["row_count"] == 1  # coerced via TRY_CAST(... AS DOUBLE)


def test_column_collision_suffixed():
    left = {"columns": ["sku", "amount"], "rows": [["A-1", "1"]]}
    right = {"columns": ["item", "amount"], "rows": [["A-1", "2"]]}
    out = join_rows(left, right, [{"left": "sku", "right": "item"}], "inner")
    assert out["columns"] == ["sku", "amount", "amount_r"]
    assert out["rows"] == [["A-1", "1", "2"]]


def test_no_match_returns_empty():
    left = {"columns": ["sku", "x"], "rows": [["A", "1"]]}
    right = {"columns": ["item", "y"], "rows": [["Z", "2"]]}
    out = join_rows(left, right, [{"left": "sku", "right": "item"}], "inner")
    assert out["rows"] == [] and out["row_count"] == 0


def test_multi_key_join():
    left = {"columns": ["region", "sku", "v"], "rows": [["EU", "A", "1"], ["US", "A", "2"]]}
    right = {"columns": ["region", "item", "w"], "rows": [["EU", "A", "9"]]}
    out = join_rows(
        left,
        right,
        [{"left": "region", "right": "region"}, {"left": "sku", "right": "item"}],
        "inner",
    )
    assert out["row_count"] == 1
    assert out["rows"][0] == ["EU", "A", "1", "9"]  # right keys (region,item) dropped


def test_invalid_join_key_raises():
    with pytest.raises(ValueError):
        join_rows(LEFT, RIGHT, [{"left": "nope", "right": "item"}], "inner")


def test_unsupported_join_type_raises():
    with pytest.raises(ValueError):
        join_rows(LEFT, RIGHT, [{"left": "sku", "right": "item"}], "cross")


def test_join_then_pivot():
    # Join produces sku/platform/qty, then pivot platform -> columns.
    left = {"columns": ["sku", "platform"], "rows": [["A", "Web"], ["B", "Retail"]]}
    right = {"columns": ["item", "qty"], "rows": [["A", "10"], ["B", "20"]]}
    out = join_rows(
        left,
        right,
        [{"left": "sku", "right": "item"}],
        "inner",
        pivot={"row_field": "sku", "column_field": "platform", "value_field": "qty"},
    )
    assert out["pivoted"] is True
    assert "Web" in out["columns"] and "Retail" in out["columns"]


def test_select_unknown_column_raises():
    with pytest.raises(ValueError):
        join_rows(LEFT, RIGHT, [{"left": "sku", "right": "item"}], "inner", select=["nope"])


def test_duplicate_output_names_disambiguated():
    # left already has 'amount' AND 'amount_r'; right 'amount' collides -> suffixed -> deduped.
    left = {"columns": ["sku", "amount", "amount_r"], "rows": [["A", "1", "x"]]}
    right = {"columns": ["item", "amount"], "rows": [["A", "2"]]}
    out = join_rows(left, right, [{"left": "sku", "right": "item"}], "inner")
    assert len(out["columns"]) == len(set(out["columns"]))  # all unique
    assert out["columns"] == ["sku", "amount", "amount_r", "amount_r_2"]


def test_inner_join_null_key_does_not_match():
    left = {"columns": ["k", "v"], "rows": [[None, "1"]]}
    right = {"columns": ["k", "w"], "rows": [[None, "2"]]}
    out = join_rows(left, right, [{"left": "k", "right": "k"}], "inner")
    assert out["row_count"] == 0  # NULL never equals NULL
