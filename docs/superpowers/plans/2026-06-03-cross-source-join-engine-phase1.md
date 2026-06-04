# Cross-Source Join Engine (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `cross_source_query` agent tool that re-runs two source queries (SuiteQL × BigQuery), joins them in an ephemeral in-memory DuckDB, and returns one unified `data_table` — so the LLM orchestrates but never does the join math.

**Architecture:** A pure, dependency-light join engine (`join_service.join_rows`) does the DuckDB work (ephemeral `:memory:` connection, `memory_limit=256MB`, `threads=1`, closed in `finally`). A thin tool (`cross_source_tool`) fetches both sources via the *existing* `pivot_tool` fetch paths (so tenant isolation + row caps are inherited), calls the engine via `asyncio.to_thread`, and returns the standard `{columns, rows}` envelope so the existing `_intercept_tool_result` `data_table` path renders it and condenses for the LLM. Wiring mirrors `pivot.query_result` exactly.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, DuckDB (new dep), pytest. Spec: `docs/superpowers/specs/2026-06-03-cross-source-join-engine-phase1.md`.

**Test command (run from the worktree so imports resolve to worktree code, not the main checkout — worktree venv `.pth` caveat):**
```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-cross-source-join-engine/backend
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/<file> -v
```
Pure tests (join engine, tool with mocks, registry/allowlist) need no DB. Run `ruff check` + `ruff format --check` before each commit (CI runs both).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend/app/services/join_service.py` | Pure DuckDB-backed join engine (`join_rows`) — no DB/network | Create |
| `backend/app/mcp/tools/cross_source_tool.py` | The tool: fetch both sources, call engine, build envelope | Create |
| `backend/tests/test_join_service.py` | Unit tests for the engine | Create |
| `backend/tests/test_cross_source_tool.py` | Tool tests (mocked fetch) + wiring tests | Create |
| `backend/app/mcp/registry.py` | Register `cross_source.query` in `TOOL_REGISTRY` | Modify |
| `backend/app/services/chat/nodes.py` | Add to `ALLOWED_CHAT_TOOLS` | Modify |
| `backend/app/services/chat/tool_categories.py` | Add to `_EXACT` (`data_table`) | Modify |
| `backend/app/services/chat/agents/unified_agent.py` | One `<tool_selection>` routing line | Modify |
| `backend/app/services/chat/knowledge_profiles/cross_source.yaml` | Rewrite step 3 + add `trigger_tools` | Modify |
| `backend/app/services/chat/prompt_assembler.py` | Rewrite `DISAMBIGUATION_INSTRUCTION` join line | Modify |
| `backend/app/services/chat/plan_mode/short_circuit.py` | Add to `_CROSS_SOURCE_TOOLS` | Modify |
| `backend/pyproject.toml` | Add `duckdb` dependency | Modify |
| `backend/Dockerfile.prod` | Verify `duckdb` installs in prod image | Verify |

---

## Task 1: Add the DuckDB dependency

**Files:**
- Modify: `backend/pyproject.toml` (+ `backend/uv.lock`)
- Test: `backend/tests/test_join_service.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/test_join_service.py`:

```python
"""Tests for the deterministic cross-source join engine."""


def test_duckdb_importable():
    import duckdb

    con = duckdb.connect(database=":memory:")
    try:
        assert con.execute("SELECT 42").fetchone()[0] == 42
    finally:
        con.close()
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError: duckdb`):

```bash
cd .../feat-cross-source-join-engine/backend
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_join_service.py::test_duckdb_importable -v
```

- [ ] **Step 3: Add the dependency** — from `backend/`:

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m uv add duckdb
```
(If `uv add` is unavailable, add `"duckdb>=1.1.0"` to `[project].dependencies` in `pyproject.toml` and run `uv lock` + `uv sync`.)

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Verify the prod image will install it.** Read `backend/Dockerfile.prod`; confirm it installs from `pyproject.toml`/`uv.lock` (e.g. `uv sync`/`uv pip install`). If it pins a copied requirements file instead, add `duckdb` there too. No code change if it syncs from `pyproject.toml`.

- [ ] **Step 6: Commit:**

```bash
git add backend/pyproject.toml backend/uv.lock backend/tests/test_join_service.py
git commit -m "build(backend): add duckdb dependency for cross-source join engine"
```

---

## Task 2: The join engine — `join_service.join_rows`

**Files:**
- Create: `backend/app/services/join_service.py`
- Test: `backend/tests/test_join_service.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_join_service.py`:

```python
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
        left, right,
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
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: app.services.join_service`):

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_join_service.py -v
```

- [ ] **Step 3: Implement** — create `backend/app/services/join_service.py`:

```python
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
    """Coerce a value to a DuckDB-insertable scalar. Everything stored as
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
```

- [ ] **Step 4: Run — expect PASS** (all 8 tests).

- [ ] **Step 5: Lint + commit:**

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff check app/services/join_service.py tests/test_join_service.py
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff format app/services/join_service.py tests/test_join_service.py
git add backend/app/services/join_service.py backend/tests/test_join_service.py
git commit -m "feat(chat): deterministic DuckDB cross-source join engine (join_service)"
```

---

## Task 3: The tool — `cross_source_tool.execute`

**Files:**
- Create: `backend/app/mcp/tools/cross_source_tool.py`
- Test: `backend/tests/test_cross_source_tool.py`

- [ ] **Step 1: Write failing tests** — `backend/tests/test_cross_source_tool.py`:

```python
"""Tests for the cross_source_query tool (mocked source fetches)."""

import pytest

from app.mcp.tools import cross_source_tool


@pytest.mark.asyncio
async def test_execute_joins_two_sources(monkeypatch):
    async def fake_run_source(query, dialect, context):
        if dialect == "suiteql":
            return {"columns": ["sku", "ns_sales"], "rows": [["A", "100"], ["B", "200"]], "truncated": False}
        return {"columns": ["item", "bq_spend"], "rows": [["A", "10"]], "truncated": False}

    monkeypatch.setattr(cross_source_tool, "_run_source", fake_run_source)

    out = await cross_source_tool.execute(
        {
            "left_query": "SELECT ...",
            "left_dialect": "suiteql",
            "right_query": "SELECT ...",
            "right_dialect": "bigquery",
            "join_keys": [{"left": "sku", "right": "item"}],
            "join_type": "inner",
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert out["joined"] is True
    assert out["row_count"] == 1
    assert out["rows"] == [["A", "100", "10"]]
    assert out["left_row_count"] == 2 and out["right_row_count"] == 1
    assert out["warnings"] == []


@pytest.mark.asyncio
async def test_execute_requires_context():
    out = await cross_source_tool.execute({"left_query": "x"}, context={})
    assert "error" in out


@pytest.mark.asyncio
async def test_execute_requires_join_keys():
    out = await cross_source_tool.execute(
        {"left_query": "a", "right_query": "b", "left_dialect": "suiteql", "right_dialect": "bigquery"},
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert "error" in out and "join_keys" in out["error"]


@pytest.mark.asyncio
async def test_execute_surfaces_source_error(monkeypatch):
    async def boom(query, dialect, context):
        raise ValueError("No active BigQuery connector")

    monkeypatch.setattr(cross_source_tool, "_run_source", boom)
    out = await cross_source_tool.execute(
        {
            "left_query": "a", "left_dialect": "bigquery",
            "right_query": "b", "right_dialect": "suiteql",
            "join_keys": [{"left": "x", "right": "y"}],
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert "error" in out and "Left source" in out["error"]


@pytest.mark.asyncio
async def test_execute_warns_on_truncation_and_no_match(monkeypatch):
    async def fake_run_source(query, dialect, context):
        if dialect == "suiteql":
            return {"columns": ["sku", "v"], "rows": [["A", "1"]], "truncated": True}
        return {"columns": ["item", "w"], "rows": [["Z", "2"]], "truncated": False}

    monkeypatch.setattr(cross_source_tool, "_run_source", fake_run_source)
    out = await cross_source_tool.execute(
        {
            "left_query": "a", "left_dialect": "suiteql",
            "right_query": "b", "right_dialect": "bigquery",
            "join_keys": [{"left": "sku", "right": "item"}],
        },
        context={"db": object(), "tenant_id": "t-1"},
    )
    assert out["left_truncated"] is True
    assert any("truncated" in w for w in out["warnings"])
    assert any("No rows matched" in w for w in out["warnings"])
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: ...cross_source_tool`).

- [ ] **Step 3: Implement** — create `backend/app/mcp/tools/cross_source_tool.py`:

```python
"""cross_source_query tool — deterministic cross-source join.

Re-runs two source queries (SuiteQL / BigQuery) to materialize full bounded
rows (tenant-filtered by the existing source paths), joins them in-process via
the DuckDB-backed engine, and returns one {columns, rows} table through the
data_table interception path. The LLM never does the join math.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any
from uuid import UUID

from app.mcp.tools.pivot_tool import _strip_row_limit
from app.services.join_service import join_rows

logger = logging.getLogger(__name__)

_MAX_ROWS_PER_SIDE = 10000


async def _run_source(query: str, dialect: str, context: dict) -> dict:
    """Fetch one source's full (bounded) rows, tenant-filtered. Mirrors pivot_tool."""
    from sqlalchemy import select

    from app.core.encryption import decrypt_credentials

    db = context.get("db")
    tenant_id_str = context.get("tenant_id")
    if not db or not tenant_id_str:
        raise ValueError("Database session and tenant_id required")
    tenant_id = UUID(tenant_id_str) if isinstance(tenant_id_str, str) else tenant_id_str

    d = "bigquery" if dialect == "bigquery" else "suiteql"
    clean = _strip_row_limit(query, dialect=d)

    if d == "bigquery":
        from app.models.mcp_connector import McpConnector
        from app.services.bigquery_service import execute_query

        res = await db.execute(
            select(McpConnector).where(
                McpConnector.tenant_id == tenant_id,
                McpConnector.provider == "bigquery",
                McpConnector.status == "active",
            )
        )
        connector = res.scalars().first()
        if not connector:
            raise ValueError("No active BigQuery connector for this tenant")
        creds = decrypt_credentials(connector.encrypted_credentials)
        sa_json = creds.get("service_account_json", {})
        project_id = creds.get("project_id") or (connector.metadata_json or {}).get("project_id", "")
        location = creds.get("location") or (connector.metadata_json or {}).get("location")
        raw = await execute_query(
            credentials=sa_json,
            project_id=project_id,
            query=clean,
            max_rows=_MAX_ROWS_PER_SIDE,
            location=location,
        )
    else:
        from app.models.connection import Connection
        from app.services.netsuite_client import execute_suiteql_via_rest
        from app.services.netsuite_oauth_service import get_valid_token

        res = await db.execute(
            select(Connection)
            .where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status == "active",
            )
            .order_by(Connection.updated_at.desc())
            .limit(1)
        )
        connection = res.scalar_one_or_none()
        if not connection:
            raise ValueError("No active NetSuite connection for this tenant")
        access_token = await get_valid_token(db, connection)
        if not access_token:
            raise ValueError("NetSuite OAuth token expired — re-authorize in Settings")
        creds = decrypt_credentials(connection.encrypted_credentials)
        account_id = creds.get("account_id", "")
        raw = await execute_suiteql_via_rest(
            access_token=access_token,
            account_id=account_id,
            query=clean,
            limit=_MAX_ROWS_PER_SIDE,
        )

    rows = raw.get("rows", []) or []
    return {
        "columns": raw.get("columns", []),
        "rows": rows,
        "truncated": bool(raw.get("truncated", False)) or len(rows) >= _MAX_ROWS_PER_SIDE,
    }


async def execute(params: dict, context: dict | None = None, **kwargs: Any) -> dict:
    """Run both source queries, join them deterministically, return one table."""
    ctx = context or {}
    if not ctx.get("db") or not ctx.get("tenant_id"):
        return {"error": "Database session and tenant_id required"}

    left_query = params.get("left_query", "")
    right_query = params.get("right_query", "")
    left_dialect = params.get("left_dialect", "suiteql")
    right_dialect = params.get("right_dialect", "suiteql")
    join_keys = params.get("join_keys") or []
    join_type = params.get("join_type", "inner")
    select = params.get("select")

    if not left_query or not right_query:
        return {"error": "left_query and right_query are required"}
    if not join_keys:
        return {"error": 'join_keys required, e.g. [{"left": "sku", "right": "item"}]'}

    try:
        left = await _run_source(left_query, left_dialect, ctx)
    except Exception as e:  # noqa: BLE001 — surface a structured error, never crash the turn
        return {"error": f"Left source ({left_dialect}) failed: {str(e)[:300]}"}
    try:
        right = await _run_source(right_query, right_dialect, ctx)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Right source ({right_dialect}) failed: {str(e)[:300]}"}

    tmpdir = os.path.join(tempfile.gettempdir(), "duckdb")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        result = await asyncio.to_thread(
            join_rows, left, right, join_keys, join_type, select, ("_l", "_r"), "256MB", tmpdir
        )
    except ValueError as e:
        return {"error": str(e)}

    warnings: list[str] = []
    if left.get("truncated"):
        warnings.append(f"Left source truncated at {_MAX_ROWS_PER_SIDE} rows — join is partial.")
    if right.get("truncated"):
        warnings.append(f"Right source truncated at {_MAX_ROWS_PER_SIDE} rows — join is partial.")
    if result["row_count"] == 0:
        warnings.append("No rows matched the join key(s) — check the join key columns.")

    result.update(
        {
            "left_row_count": len(left["rows"]),
            "right_row_count": len(right["rows"]),
            "left_truncated": left.get("truncated", False),
            "right_truncated": right.get("truncated", False),
            "warnings": warnings,
        }
    )
    return result
```

- [ ] **Step 4: Run — expect PASS** (5 tests).

- [ ] **Step 5: Lint + commit:**

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff check app/mcp/tools/cross_source_tool.py tests/test_cross_source_tool.py
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff format app/mcp/tools/cross_source_tool.py tests/test_cross_source_tool.py
git add backend/app/mcp/tools/cross_source_tool.py backend/tests/test_cross_source_tool.py
git commit -m "feat(chat): cross_source_query tool — re-run both sources + DuckDB join"
```

---

## Task 4: Register the tool (registry + allowlist + category)

**Files:**
- Modify: `backend/app/mcp/registry.py:1-26` (import), `:86-133` region (add entry after `pivot.query_result`)
- Modify: `backend/app/services/chat/nodes.py:31-63`
- Modify: `backend/app/services/chat/tool_categories.py:26-65`
- Test: `backend/tests/test_cross_source_tool.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_cross_source_tool.py`:

```python
def test_tool_registered():
    from app.mcp.registry import TOOL_REGISTRY

    assert "cross_source.query" in TOOL_REGISTRY
    schema = TOOL_REGISTRY["cross_source.query"]["params_schema"]
    for p in ("left_query", "left_dialect", "right_query", "right_dialect", "join_keys"):
        assert p in schema


def test_tool_in_allowlist():
    from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

    assert "cross_source.query" in ALLOWED_CHAT_TOOLS


def test_tool_categorized_data_table():
    from app.services.chat.tool_categories import categorize

    assert categorize("cross_source_query") == "data_table"
    assert categorize("cross_source.query") == "data_table"
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3a: Register in `registry.py`.** Add `cross_source_tool` to the import tuple (`registry.py:1-26`, alphabetical — after `bigquery_tools`/`data_sample`, before `pivot_tool`). Then add this entry to `TOOL_REGISTRY` immediately after the `pivot.query_result` block (after line 133):

```python
    "cross_source.query": {
        "description": (
            "Join data across TWO sources (NetSuite SuiteQL and BigQuery) into one "
            "unified table. Pass both queries + the join key; the backend re-runs both, "
            "joins them deterministically (DuckDB), and returns one table. Use this "
            "INSTEAD of correlating two separate result tables yourself."
        ),
        "execute": cross_source_tool.execute,
        "params_schema": {
            "left_query": {"type": "string", "required": True, "description": "SQL for source A"},
            "left_dialect": {
                "type": "string", "required": True,
                "description": "'suiteql' or 'bigquery' for source A",
            },
            "right_query": {"type": "string", "required": True, "description": "SQL for source B"},
            "right_dialect": {
                "type": "string", "required": True,
                "description": "'suiteql' or 'bigquery' for source B",
            },
            "join_keys": {
                "type": "array", "required": True,
                "description": 'Equality keys, e.g. [{"left": "sku", "right": "item"}]',
            },
            "join_type": {
                "type": "string", "required": False, "default": "inner",
                "description": "'inner' or 'left'",
            },
            "select": {
                "type": "array", "required": False,
                "description": "Optional output columns to keep (default: all)",
            },
        },
    },
```

- [ ] **Step 3b: Allowlist** — `nodes.py`, add `"cross_source.query",` to `ALLOWED_CHAT_TOOLS` right after `"pivot.query_result",` (line 34).

- [ ] **Step 3c: Category** — `tool_categories.py`, add to `_EXACT` right after the `pivot.query_result` lines (line 30):

```python
    "cross_source_query": "data_table",
    "cross_source.query": "data_table",
```

- [ ] **Step 4: Run — expect PASS** (3 tests). Then run the whole tool test file.

- [ ] **Step 5: Lint + commit:**

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff check app/mcp/registry.py app/services/chat/nodes.py app/services/chat/tool_categories.py
git add backend/app/mcp/registry.py backend/app/services/chat/nodes.py backend/app/services/chat/tool_categories.py backend/tests/test_cross_source_tool.py
git commit -m "feat(chat): register cross_source_query tool (registry + allowlist + data_table category)"
```

---

## Task 5: Prompt wiring (route the LLM to the tool) + CI sync

**Files:**
- Modify: `backend/app/services/chat/agents/unified_agent.py` (`<tool_selection>` block)
- Modify: `backend/app/services/chat/knowledge_profiles/cross_source.yaml`
- Modify: `backend/app/services/chat/prompt_assembler.py` (`DISAMBIGUATION_INSTRUCTION`)
- Modify: `backend/app/services/chat/plan_mode/short_circuit.py` (`_CROSS_SOURCE_TOOLS`)
- Test: `backend/tests/test_prompt_tool_sync.py` (existing — must stay green)

- [ ] **Step 1: Run the sync test first (baseline green):**

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_prompt_tool_sync.py -v
```

- [ ] **Step 2: `cross_source.yaml`** — replace step 3 (line 12). Read the file; change:
> `3. Correlate the results in your response — present a unified answer, not two separate tables`

to:
> `3. Call cross_source_query with both queries + the join key — it joins the two sources and returns ONE unified table (rendered automatically; do NOT re-list the numbers).`

Also add `cross_source_query` to that profile's `trigger_tools:` list (keep `bigquery_sql`, `netsuite_suiteql`).

- [ ] **Step 3: `prompt_assembler.py`** — in `DISAMBIGUATION_INSTRUCTION` (~lines 22-23), replace:
> `If the question clearly requires data from both sources, call both tools and synthesize the results.`
> `Identify the join key (SKU, customer email, order ID, date range) to correlate cross-source data.`

with:
> `If the question requires BOTH sources, call cross_source_query (pass both queries + the join key — SKU, customer email, order ID, or date range). It merges them into one table; do NOT correlate two tables by hand.`

- [ ] **Step 4: `unified_agent.py`** — in the `<tool_selection>` block, add one line near the other source-routing guidance:
> `- Cross-source (NetSuite × BigQuery in one answer): use cross_source_query (both queries + join key) — never eyeball two tables.`

- [ ] **Step 5: `short_circuit.py`** — add `"cross_source_query"` to the `_CROSS_SOURCE_TOOLS` set (next to `"pivot_query_result"`).

- [ ] **Step 6: Run the sync test + a prompt-render smoke — expect PASS:**

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_prompt_tool_sync.py -v
```
Expected: PASS (the tool is registered, so naming it in the prompt is allowed; `{{TOOL_INVENTORY}}` auto-renders its description).

- [ ] **Step 7: Commit:**

```bash
git add backend/app/services/chat/agents/unified_agent.py \
        backend/app/services/chat/knowledge_profiles/cross_source.yaml \
        backend/app/services/chat/prompt_assembler.py \
        backend/app/services/chat/plan_mode/short_circuit.py
git commit -m "feat(chat): route the agent to cross_source_query (profile + disambiguation + tool-selection + plan-mode)"
```

---

## Task 6: Optional pivot pass-through (reuse `pivot_rows` verbatim)

**Files:**
- Modify: `backend/app/services/join_service.py`
- Test: `backend/tests/test_join_service.py`

- [ ] **Step 1: Write the failing test** — append to `test_join_service.py`:

```python
def test_join_then_pivot():
    # Join produces region/platform/qty, then pivot platform -> columns.
    left = {"columns": ["sku", "platform"], "rows": [["A", "Web"], ["B", "Retail"]]}
    right = {"columns": ["item", "qty"], "rows": [["A", "10"], ["B", "20"]]}
    out = join_rows(
        left, right, [{"left": "sku", "right": "item"}], "inner",
        pivot={"row_field": "sku", "column_field": "platform", "value_field": "qty"},
    )
    assert out["pivoted"] is True
    assert "Web" in out["columns"] and "Retail" in out["columns"]
```

- [ ] **Step 2: Run — expect FAIL** (`join_rows() got an unexpected keyword 'pivot'`).

- [ ] **Step 3: Implement** — add a `pivot: dict | None = None` keyword param to `join_rows` (after `select`). After building `out_columns`/`out_rows` and applying `select`, before the return:

```python
    if pivot:
        from app.services.pivot_service import pivot_rows

        out_columns, out_rows = pivot_rows(
            columns=out_columns,
            rows=out_rows,
            row_field=pivot["row_field"],
            column_field=pivot["column_field"],
            value_field=pivot["value_field"],
            aggregation=pivot.get("aggregation", "sum"),
            include_total=pivot.get("include_total", True),
        )
        return {
            "columns": out_columns, "rows": out_rows, "row_count": len(out_rows),
            "joined": True, "join_type": join_type, "pivoted": True,
        }
```

Then expose it in the tool: in `cross_source_tool.execute`, read `pivot = params.get("pivot")` and pass it through the `asyncio.to_thread(join_rows, ...)` call (add as the next positional/keyword arg), and add a `"pivot"` entry to the registry `params_schema` (object, optional). Update the existing `to_thread` call to pass `pivot=pivot` as a keyword to avoid positional drift.

- [ ] **Step 4: Run — expect PASS.** Re-run the full `test_join_service.py` + `test_cross_source_tool.py`.

- [ ] **Step 5: Lint + commit:**

```bash
git add backend/app/services/join_service.py backend/app/mcp/tools/cross_source_tool.py backend/app/mcp/registry.py backend/tests/test_join_service.py
git commit -m "feat(chat): optional pivot pass-through on cross-source join (reuses pivot_rows)"
```

---

## Task 7: Full suite, lint/format gate, benchmark

**Files:** none (verification).

- [ ] **Step 1: Full backend suite** (from `backend/`):

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest -q
```
Expected: all green (no regressions). If DB-backed tests need it: `docker compose up -d postgres backend` first (auto-migrates) per the local test harness.

- [ ] **Step 2: Lint + format gate** (CI runs BOTH):

```bash
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff check .
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m ruff format --check .
```

- [ ] **Step 3: Benchmark gate.** Run the vs-Claude+MCP benchmark per `memory/feedback_benchmark_vs_claude_mcp.md` and confirm the cross-source scenario is ≥ baseline. (This is the CLAUDE.md north-star CI gate.)

- [ ] **Step 4: Final review.** Confirm: no Alembic migration was added (Phase 1 has none); `duckdb` is in `pyproject.toml` + `uv.lock` + installs in `Dockerfile.prod`; the tool emits `data_table` (LLM never sees raw joined numbers); `git log --oneline` shows one commit per logical change.

---

## Self-Review (against the spec)

**Spec coverage:** tool contract (Task 3 + Task 4 registry schema) ✓; re-run model (Task 3 `_run_source`) ✓; DuckDB engine + runtime safety — `memory_limit`/`threads`/`temp_directory`/ephemeral close/`to_thread` (Task 2 + Task 3) ✓; `data_table` trust boundary (Task 4 category) ✓; tenant isolation via existing source paths (Task 3) ✓; wiring 5 required + 2 optional (Tasks 4-5) ✓; dependency (Task 1) ✓; both prompt surfaces (Task 5) ✓; truncation/no-match/error handling (Task 3 tests) ✓; optional pivot (Task 6) ✓; TDD throughout ✓; acceptance/benchmark (Task 7) ✓.

**Deferred (per spec non-goals):** Drive as a join source (Phase 3); metric catalog (Phase 2); 3-normalizer unification; full-result persistence; standalone aggregations param (source-level GROUP BY + the pivot pass-through cover Phase 1); 3-way joins.

**Type consistency:** `join_rows(left, right, join_keys, join_type, select, suffixes, memory_limit, temp_directory, pivot)`; envelope keys `{columns, rows, row_count, joined, join_type, (pivoted), left_row_count, right_row_count, left_truncated, right_truncated, warnings}` consistent across engine ↔ tool ↔ tests. Tool name `cross_source.query` (registry) / `cross_source_query` (LLM) consistent.

**Open questions resolved as:** row cap 10k/side (`_MAX_ROWS_PER_SIDE`), warn-not-refuse on truncation; raw queries only; collision suffix `_r`; join + optional pivot in scope; `temp_directory` = `<tmp>/duckdb`.
