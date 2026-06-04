"""Deterministic cross-source join engine (DuckDB-backed).

Joins two already-fetched result sets ({columns, rows}) in an ephemeral
in-memory DuckDB. Pure compute: no DB session, no network. The LLM never
does the join — this is the deterministic backend that does.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_JOIN_TYPES = {"inner": "INNER", "left": "LEFT"}


def _q(ident: str) -> str:
    """Quote a DuckDB identifier safely (double quotes, doubled internally)."""
    return '"' + str(ident).replace('"', '""') + '"'


def _to_cell(v: Any) -> Any:
    """Coerce a value to a DuckDB-insertable scalar. Everything is stored as
    VARCHAR (or None); numeric join coercion happens in the ON clause."""
    if v is None:
        return None
    return str(v)


def join_rows(
    left: dict,
    right: dict,
    join_keys: list[dict],
    join_type: str = "inner",
    select: list[str] | None = None,
    suffixes: tuple[str, str] = ("_l", "_r"),
    memory_limit: str = "256MB",
    temp_directory: str | None = None,
) -> dict:
    """Join two {columns, rows} result sets deterministically.

    join_keys: [{"left": "<left col>", "right": "<right col>"}, ...]
    join_type: "inner" | "left". Returns {columns, rows, row_count, joined, join_type}.
    """
    import duckdb

    left_cols = list(left.get("columns", []))
    right_cols = list(right.get("columns", []))
    left_rows = left.get("rows", []) or []
    right_rows = right.get("rows", []) or []

    if not join_keys:
        raise ValueError("join_keys required")
    jt = _JOIN_TYPES.get(join_type)
    if jt is None:
        raise ValueError(f"unsupported join_type '{join_type}' (use inner|left)")
    for k in join_keys:
        if k.get("left") not in left_cols:
            raise ValueError(f"left join key '{k.get('left')}' not in columns: {left_cols}")
        if k.get("right") not in right_cols:
            raise ValueError(f"right join key '{k.get('right')}' not in columns: {right_cols}")

    # Output columns: all left columns, then right columns except the join keys,
    # suffixing any name that collides with a left column.
    key_right = {k["right"] for k in join_keys}
    left_set = set(left_cols)
    out_specs: list[tuple[str, str, str]] = [("l", c, c) for c in left_cols]
    for c in right_cols:
        if c in key_right:
            continue
        out_name = c if c not in left_set else f"{c}{suffixes[1]}"
        out_specs.append(("r", c, out_name))

    con = duckdb.connect(database=":memory:")
    try:
        con.execute(f"SET memory_limit='{memory_limit}'")
        con.execute("SET threads=1")
        if temp_directory:
            con.execute(f"SET temp_directory='{temp_directory}'")

        for tbl, cols, rows in (("l", left_cols, left_rows), ("r", right_cols, right_rows)):
            coldefs = ", ".join(f"{_q(c)} VARCHAR" for c in cols) or '"_empty" VARCHAR'
            con.execute(f"CREATE TEMP TABLE {tbl} ({coldefs})")
            if cols and rows:
                placeholders = ", ".join(["?"] * len(cols))
                con.executemany(
                    f"INSERT INTO {tbl} VALUES ({placeholders})",
                    [[_to_cell(v) for v in row] for row in rows],
                )

        on_clause = " AND ".join(
            f"(l.{_q(k['left'])} = r.{_q(k['right'])} "
            f"OR TRY_CAST(l.{_q(k['left'])} AS DOUBLE) = TRY_CAST(r.{_q(k['right'])} AS DOUBLE))"
            for k in join_keys
        )
        select_sql = ", ".join(f"{side}.{_q(src)} AS {_q(out)}" for side, src, out in out_specs)
        sql = f"SELECT {select_sql} FROM l {jt} JOIN r ON {on_clause}"
        cur = con.execute(sql)
        out_columns = [d[0] for d in cur.description]
        out_rows = [list(r) for r in cur.fetchall()]
    finally:
        con.close()

    if select:
        keep = [i for i, c in enumerate(out_columns) if c in select]
        out_columns = [out_columns[i] for i in keep]
        out_rows = [[r[i] for i in keep] for r in out_rows]

    return {
        "columns": out_columns,
        "rows": out_rows,
        "row_count": len(out_rows),
        "joined": True,
        "join_type": join_type,
    }
