"""Regression tests for the vs-MCP benchmark task's emitter=None path.

The nightly Beat triggers pass emitter=None; the agent-lab Celery wrapper
passes a real ProgressEmitter. Both paths must coexist without code changes
to the underlying function body beyond the emitter kwarg + emit calls at
case boundaries.
"""

import inspect

import pytest


def test_run_nightly_benchmark_accepts_emitter_kwarg_none():
    from app.workers.tasks.agent_benchmark_vs_mcp import _run_nightly_benchmark

    sig = inspect.signature(_run_nightly_benchmark)
    assert "emitter" in sig.parameters
    assert sig.parameters["emitter"].default is None


@pytest.mark.asyncio
async def test_run_nightly_benchmark_return_dict_shape_preserved(monkeypatch):
    """Confirm return dict has expected keys regardless of emitter.

    Patches all heavy I/O (DB session, load_cases, benchmark runner,
    email digest) so the function runs to completion without network calls.
    The emitter=None path must preserve the existing return-dict contract.
    """
    from unittest.mock import AsyncMock, MagicMock

    # Patch load_cases at its definition module (imported inside the function)
    monkeypatch.setattr(
        "app.services.benchmarks.run_vs_mcp.load_cases",
        lambda suite: [],
    )

    # Patch _get_avg_delta_for_date at the task module level
    from app.workers.tasks import agent_benchmark_vs_mcp as m

    # Return 0.0 rather than None — the pre-existing print statement at line ~254
    # uses `:+.3f` format on yesterday_delta before the None-guard, so None crashes.
    monkeypatch.setattr(m, "_get_avg_delta_for_date", AsyncMock(return_value=0.0))

    # Suppress email digest (imported inside function)
    monkeypatch.setattr(
        "app.services.benchmark_email_service.send_benchmark_digest",
        MagicMock(),
    )

    # Fake async DB session context manager
    class _FakeDB:
        async def execute(self, *args, **kwargs):
            r = MagicMock()
            r.first.return_value = None
            return r

        async def commit(self):
            pass

        async def rollback(self):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *args):
            pass

    # Patch async_session_factory and set_tenant_context at the source module
    monkeypatch.setattr("app.core.database.async_session_factory", lambda: _FakeCtx())
    monkeypatch.setattr("app.core.database.set_tenant_context", AsyncMock())

    from uuid import UUID

    result = await m._run_nightly_benchmark(
        tenant_id=UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a"),
        suite="sales",
        agent_model="claude-haiku-4-5-20251001",
        baseline_model="claude-haiku-4-5-20251001",
        emitter=None,
    )

    required_keys = {
        "run_id",
        "run_date",
        "cases_total",
        "cases_run",
        "ours_wins",
        "mcp_wins",
        "ties",
        "failures",
        "avg_delta_accuracy",
    }
    assert required_keys.issubset(result.keys())


def test_run_nightly_benchmark_accepts_run_id_kwarg():
    """_run_nightly_benchmark must declare an optional run_id parameter."""
    import inspect

    from app.workers.tasks.agent_benchmark_vs_mcp import _run_nightly_benchmark

    sig = inspect.signature(_run_nightly_benchmark)
    assert "run_id" in sig.parameters
    param = sig.parameters["run_id"]
    assert param.default is None


@pytest.mark.asyncio
async def test_run_nightly_benchmark_uses_provided_run_id(monkeypatch):
    """When run_id is passed, persist_case_result must be called with it.

    This verifies the cost-reconciliation fix: benchmark rows keyed to
    the agent_lab_run.id rather than an internally-generated UUID.
    """
    import uuid
    from unittest.mock import AsyncMock, MagicMock, call

    provided_run_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    # We need at least one case so the persist path is exercised.
    fake_case = MagicMock()
    fake_case.case_id = "test_case_001"
    monkeypatch.setattr(
        "app.services.benchmarks.run_vs_mcp.load_cases",
        lambda suite: [fake_case],
    )

    from app.workers.tasks import agent_benchmark_vs_mcp as m

    monkeypatch.setattr(m, "_get_avg_delta_for_date", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        "app.services.benchmark_email_service.send_benchmark_digest",
        MagicMock(),
    )

    # Fake a single successful case result
    fake_ours = MagicMock(answer_acc=0.9, tool_acc=0.8)
    fake_mcp = MagicMock(answer_acc=0.7, tool_acc=0.6)
    fake_result = MagicMock(
        verdict="OURS WINS",
        ours=fake_ours,
        mcp=fake_mcp,
        ours_raw={"answer": "a"},
        mcp_raw={"answer": "b"},
    )
    monkeypatch.setattr(
        "app.services.benchmarks.run_vs_mcp._run_single_case",
        AsyncMock(return_value=fake_result),
    )

    persist_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.benchmarks.persistence.persist_case_result",
        persist_mock,
    )

    class _FakeDB:
        async def execute(self, *args, **kwargs):
            r = MagicMock()
            r.first.return_value = None
            return r

        async def commit(self):
            pass

        async def rollback(self):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("app.core.database.async_session_factory", lambda: _FakeCtx())
    monkeypatch.setattr("app.core.database.set_tenant_context", AsyncMock())

    result = await m._run_nightly_benchmark(
        tenant_id=uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a"),
        suite="sales",
        agent_model="claude-haiku-4-5-20251001",
        baseline_model="claude-haiku-4-5-20251001",
        emitter=None,
        run_id=provided_run_id,
    )

    # The returned run_id in stats must match what was provided
    assert result["run_id"] == str(provided_run_id)

    # All persist_case_result calls must use the provided run_id (not a new UUID)
    assert persist_mock.call_count >= 1
    for call_kwargs in [c.kwargs for c in persist_mock.call_args_list]:
        assert call_kwargs["run_id"] == provided_run_id, (
            f"persist_case_result received run_id={call_kwargs['run_id']!r}, "
            f"expected {provided_run_id!r}"
        )
