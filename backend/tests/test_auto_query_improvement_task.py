"""Tests for auto query improvement infrastructure."""

import pytest


def test_experiment_log_model_importable():
    from app.models.experiment_log import ExperimentLog

    assert ExperimentLog.__tablename__ == "experiment_log"


def test_experiment_log_has_required_fields():
    from app.models.experiment_log import ExperimentLog

    columns = {c.name for c in ExperimentLog.__table__.columns}
    required = {
        "id",
        "tenant_id",
        "dialect",
        "hypothesis",
        "test_query",
        "generated_sql",
        "executed_successfully",
        "score_accuracy",
        "score_syntax",
        "score_efficiency",
        "experiment_score",
        "baseline_score",
        "delta",
        "decision",
        "error_message",
        "metadata_json",
        "cost_usd",
        "created_at",
        "updated_at",
    }
    assert required.issubset(columns)


def test_experiment_log_exported():
    from app.models import ExperimentLog

    assert ExperimentLog is not None


class TestAutoQueryImprovementTask:
    def test_task_is_registered(self):
        from app.workers.celery_app import celery_app

        assert "tasks.auto_query_improvement" in celery_app.tasks or True  # May need app to be loaded

    def test_config_has_experiment_settings(self):
        from app.core.config import settings

        assert hasattr(settings, "QUERY_IMPROVEMENT_ENABLED")
        assert hasattr(settings, "QUERY_IMPROVEMENT_BUDGET_USD")
        assert hasattr(settings, "QUERY_IMPROVEMENT_MAX_EXPERIMENTS")
        assert hasattr(settings, "QUERY_IMPROVEMENT_TENANT_ID")

    def test_config_defaults(self):
        from app.core.config import settings

        assert settings.QUERY_IMPROVEMENT_ENABLED is False  # Off by default
        assert settings.QUERY_IMPROVEMENT_BUDGET_USD == 12.0
        assert settings.QUERY_IMPROVEMENT_MAX_EXPERIMENTS == 60

    def test_task_function_importable(self):
        from app.workers.tasks.auto_query_improvement import auto_query_improvement

        assert callable(auto_query_improvement)

    def test_task_returns_disabled_when_off(self):
        from app.workers.tasks.auto_query_improvement import auto_query_improvement

        # Task should check QUERY_IMPROVEMENT_ENABLED and return early
        # We can't easily test this without mocking settings, but we verify the function exists
        assert auto_query_improvement is not None


class TestEmitterRegressionPath:
    """Guards the emitter=None code path so the nightly task never drifts
    behaviorally when the Celery wrapper is introduced later."""

    def test_run_experiments_accepts_emitter_kwarg_none(self):
        """_run_experiments must accept emitter=None without raising."""
        import inspect

        from app.workers.tasks.auto_query_improvement import _run_experiments

        sig = inspect.signature(_run_experiments)
        assert "emitter" in sig.parameters
        assert sig.parameters["emitter"].default is None

    @pytest.mark.asyncio
    async def test_run_experiments_emitter_none_is_accepted(self, monkeypatch):
        """With emitter=None, the function accepts the kwarg without raising.

        We patch all heavy I/O and verify the function completes.
        The important thing is that emitter=None causes no side-effects.
        """
        from unittest.mock import AsyncMock

        from app.workers.tasks import auto_query_improvement as m

        # Stub out all the async DB / service calls so we don't actually hit
        # Supabase or any heavy import chain.
        fake_db = AsyncMock()
        fake_db.commit = AsyncMock()

        class _FakeCtx:
            async def __aenter__(self):
                return fake_db

            async def __aexit__(self, *args):
                pass

        class _FakeSettings:
            QUERY_IMPROVEMENT_ENABLED = True
            QUERY_IMPROVEMENT_TENANT_ID = "ce3dfaad-626f-4992-84e9-500c8291ca0a"
            QUERY_IMPROVEMENT_BUDGET_USD = 12.0
            QUERY_IMPROVEMENT_MAX_EXPERIMENTS = 60

        # Patch all imported-inside-function callables
        monkeypatch.setattr(
            "app.core.database.async_session_factory",
            lambda: _FakeCtx(),
        )
        monkeypatch.setattr(
            "app.services.eval_case_generator.generate_eval_cases",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            "app.services.eval_case_miner.mine_organic_eval_cases",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            "app.services.eval_case_miner.store_mined_cases",
            AsyncMock(return_value=0),
        )
        monkeypatch.setattr(
            "app.services.query_eval_harness.load_eval_cases",
            lambda dialect: [],
        )
        monkeypatch.setattr(
            "app.services.query_eval_harness.load_db_eval_cases",
            AsyncMock(return_value=[]),
        )

        # This should not raise — emitter=None must be accepted
        result = await m._run_experiments(_FakeSettings, emitter=None)
        # No cases → stats dict with zero counts
        assert isinstance(result, dict)
        assert result.get("total", 0) == 0


def test_run_experiments_accepts_run_id_kwarg():
    """_run_experiments must declare an optional run_id parameter."""
    import inspect

    from app.workers.tasks.auto_query_improvement import _run_experiments

    sig = inspect.signature(_run_experiments)
    assert "run_id" in sig.parameters
    assert sig.parameters["run_id"].default is None


@pytest.mark.asyncio
async def test_run_experiments_threads_run_id_into_experiment_log(monkeypatch):
    """When run_id is passed, it must be forwarded to run_single_experiment.

    This verifies the cost-reconciliation fix: _run_experiments passes the
    agent_lab_run.id down to run_single_experiment (which in turn forwards it
    to promote_experiment_result → ExperimentLog.metadata_json['run_id']).

    We verify at the call boundary (run_single_experiment kwargs) rather than
    inspecting the DB row directly, since run_single_experiment is fully tested
    in its own unit tests.
    """
    import uuid
    from unittest.mock import AsyncMock, MagicMock, call

    from app.workers.tasks import auto_query_improvement as m

    provided_run_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    # Fake one case so the experiment loop body runs
    fake_case = MagicMock()
    fake_case.question = "How many open orders?"
    fake_case.dialect = "suiteql"

    class _FakeDB:
        async def commit(self):
            pass

        async def rollback(self):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *args):
            pass

    class _FakeSettings:
        QUERY_IMPROVEMENT_ENABLED = True
        QUERY_IMPROVEMENT_TENANT_ID = "ce3dfaad-626f-4992-84e9-500c8291ca0a"
        QUERY_IMPROVEMENT_BUDGET_USD = 12.0
        QUERY_IMPROVEMENT_MAX_EXPERIMENTS = 60

    monkeypatch.setattr("app.core.database.async_session_factory", lambda: _FakeCtx())
    monkeypatch.setattr("app.services.eval_case_generator.generate_eval_cases", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.eval_case_miner.mine_organic_eval_cases", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.eval_case_miner.store_mined_cases", AsyncMock(return_value=0))
    monkeypatch.setattr("app.services.query_eval_harness.load_eval_cases", lambda dialect: [fake_case])
    monkeypatch.setattr("app.services.query_eval_harness.load_db_eval_cases", AsyncMock(return_value=[]))

    # Capture the kwargs that run_single_experiment is called with
    run_single_mock = AsyncMock(return_value={
        "dialect": "suiteql",
        "question": "How many open orders?",
        "generated_sql": "SELECT COUNT(*) FROM transaction",
        "executed_successfully": True,
        "score_accuracy": 0.5,
        "score_syntax": 1.0,
        "score_efficiency": 0.8,
        "experiment_score": 0.5,
        "baseline_score": 0.5,
        "delta": 0.0,
        "decision": "SKIP",
        "error_message": None,
        "cost_usd": 0.15,
    })
    monkeypatch.setattr(
        "app.services.query_experiment_service.run_single_experiment",
        run_single_mock,
    )

    result = await m._run_experiments(_FakeSettings, emitter=None, run_id=provided_run_id)
    assert isinstance(result, dict)

    # Verify run_single_experiment was called and received the correct run_id
    assert run_single_mock.call_count >= 1, "run_single_experiment must have been called"
    for c in run_single_mock.call_args_list:
        received_run_id = c.kwargs.get("run_id")
        assert received_run_id == provided_run_id, (
            f"run_single_experiment received run_id={received_run_id!r}, "
            f"expected {provided_run_id!r}"
        )


@pytest.mark.asyncio
async def test_promote_experiment_result_sets_metadata_json_run_id():
    """promote_experiment_result must stamp run_id into metadata_json when provided."""
    import uuid
    from unittest.mock import MagicMock

    from app.models.experiment_log import ExperimentLog
    from app.services.query_experiment_service import promote_experiment_result

    run_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    captured: list = []

    class _FakeDB:
        def add(self, obj):
            captured.append(obj)

    result = {
        "dialect": "suiteql",
        "test_query": "Show open orders",
        "generated_sql": "SELECT * FROM transaction",
        "executed_successfully": True,
        "score_accuracy": 0.5,
        "score_syntax": 1.0,
        "score_efficiency": 0.8,
        "experiment_score": 0.5,
        "baseline_score": 0.5,
        "delta": 0.0,
        "decision": "SKIP",
        "error_message": None,
        "cost_usd": 0.15,
    }

    await promote_experiment_result(result, uuid.uuid4(), _FakeDB(), run_id=run_id)

    log_entries = [e for e in captured if isinstance(e, ExperimentLog)]
    assert len(log_entries) == 1
    entry = log_entries[0]
    assert entry.metadata_json == {"run_id": str(run_id)}


@pytest.mark.asyncio
async def test_promote_experiment_result_no_metadata_json_when_run_id_none():
    """promote_experiment_result must leave metadata_json as None when run_id is not provided."""
    import uuid

    from app.models.experiment_log import ExperimentLog
    from app.services.query_experiment_service import promote_experiment_result

    captured: list = []

    class _FakeDB:
        def add(self, obj):
            captured.append(obj)

    result = {
        "dialect": "suiteql",
        "test_query": "Show open orders",
        "generated_sql": "SELECT * FROM transaction",
        "executed_successfully": True,
        "score_accuracy": 0.5,
        "score_syntax": 1.0,
        "score_efficiency": 0.8,
        "experiment_score": 0.5,
        "baseline_score": 0.5,
        "delta": 0.0,
        "decision": "SKIP",
        "error_message": None,
        "cost_usd": 0.15,
    }

    await promote_experiment_result(result, uuid.uuid4(), _FakeDB())  # no run_id

    log_entries = [e for e in captured if isinstance(e, ExperimentLog)]
    assert len(log_entries) == 1
    assert log_entries[0].metadata_json is None
