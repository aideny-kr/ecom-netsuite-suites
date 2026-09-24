"""The vs-MCP CLI must say when it cannot measure anything, instead of passing or failing.

In CI the database holds no tenant data, so the agent sees no data source and every
case ends in a source question. Before 2026-09-23 that scored 0.00 and still passed;
with the source reply it fails every case. Neither is a measurement.
"""

from __future__ import annotations

import argparse
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.benchmarks import run_vs_mcp

_TENANT = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        case=None,
        suite="sales",
        tenant_id=str(_TENANT),
        agent_model="claude-sonnet-5",
        baseline_model="claude-sonnet-5",
        skip_baseline=True,
        persist=True,
        no_llm_judge=True,
    )


@asynccontextmanager
async def _fake_session():
    yield MagicMock()


def _patched(sources: dict[str, str], run_case: AsyncMock):
    case = SimpleNamespace(case_id="sales_top_customers", query="Top 10 customers this quarter?")
    return (
        patch.object(run_vs_mcp, "load_cases", return_value=[case]),
        patch.object(run_vs_mcp, "_agent_data_sources", new=AsyncMock(return_value=sources)),
        patch.object(run_vs_mcp, "_run_single_case", new=run_case),
        patch.object(run_vs_mcp, "_print_results_table"),
        patch.object(run_vs_mcp, "_print_summary"),
        patch("app.core.database.async_session_factory", _fake_session),
        patch("app.core.database.set_tenant_context", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_a_tenant_with_no_data_source_is_reported_not_measured(capsys):
    run_case = AsyncMock()
    p = _patched({}, run_case)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        code = await run_vs_mcp._main_async(_args())

    assert code == 0
    assert "NOT MEASURED" in capsys.readouterr().out
    run_case.assert_not_awaited()  # nothing run, so nothing persisted


@pytest.mark.asyncio
async def test_a_tenant_with_a_data_source_runs_its_cases():
    ok = SimpleNamespace(verdict="OURS WINS", ours=SimpleNamespace(error=None), mcp=None, ours_raw=None, mcp_raw=None)
    run_case = AsyncMock(return_value=ok)
    p = _patched({"netsuite": "NetSuite"}, run_case)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        code = await run_vs_mcp._main_async(_args())

    assert code == 0
    run_case.assert_awaited_once()
