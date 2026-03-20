"""Server-side pivot for SuiteQL query results.

Deterministic pivoting — only values that exist in the data become columns.
No LLM judgment, no hallucinated values, no dropped variants.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def pivot_rows(
    columns: list[str],
    rows: list[list],
    row_field: str,
    column_field: str,
    value_field: str,
    aggregation: str = "sum",
    include_total: bool = True,
) -> tuple[list[str], list[list]]:
    """Pivot flat query results into a crosstab table.

    Parameters
    ----------
    columns : list[str]
        Column names from the query result.
    rows : list[list]
        Row data from the query result.
    row_field : str
        Column name for row grouping (e.g., "week_start_date").
    column_field : str
        Column name whose distinct values become pivot columns (e.g., "platform").
    value_field : str
        Column name to aggregate into cells (e.g., "total_qty").
    aggregation : str
        "sum", "count", "avg", "max", "min"
    include_total : bool
        Add a "Total" column summing all pivot columns per row.

    Returns
    -------
    tuple[list[str], list[list]]
        (output_columns, output_rows) ready for DataFrameTable rendering.
    """
    # Validate field names
    for field, label in [(row_field, "row_field"), (column_field, "column_field"), (value_field, "value_field")]:
        if field not in columns:
            raise ValueError(f"{label} '{field}' not found in columns: {columns}")

    if not rows:
        return [row_field], []

    row_idx = columns.index(row_field)
    col_idx = columns.index(column_field)
    val_idx = columns.index(value_field)

    # Collect distinct column values, sorted alphabetically
    seen_cols: set[str] = set()
    for row in rows:
        val = str(row[col_idx]) if row[col_idx] is not None else ""
        seen_cols.add(val)
    pivot_cols = sorted(seen_cols)

    # Build pivot: {row_key: {col_value: [values]}}
    pivot: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    row_order: dict[str, None] = {}
    for row in rows:
        rk = str(row[row_idx]) if row[row_idx] is not None else ""
        ck = str(row[col_idx]) if row[col_idx] is not None else ""
        try:
            v = float(row[val_idx]) if row[val_idx] is not None else 0.0
        except (ValueError, TypeError):
            v = 0.0
        pivot[rk][ck].append(v)
        if rk not in row_order:
            row_order[rk] = None

    # Aggregation function
    agg_fns = {
        "sum": sum,
        "count": len,
        "avg": lambda x: sum(x) / len(x) if x else 0.0,
        "max": lambda x: max(x) if x else 0.0,
        "min": lambda x: min(x) if x else 0.0,
    }
    agg_fn = agg_fns.get(aggregation, sum)

    # Build output
    out_columns = [row_field] + pivot_cols + (["Total"] if include_total else [])
    out_rows: list[list] = []

    for rk in row_order:
        row_data: list[Any] = [rk]
        total = 0.0
        for pc in pivot_cols:
            val = agg_fn(pivot[rk][pc]) if pivot[rk][pc] else 0.0
            row_data.append(val)
            if isinstance(val, (int, float)):
                total += val
        if include_total:
            row_data.append(total)
        out_rows.append(row_data)

    return out_columns, out_rows
