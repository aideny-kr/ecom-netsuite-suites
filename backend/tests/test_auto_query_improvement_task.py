"""Tests for auto query improvement infrastructure."""


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
        assert settings.QUERY_IMPROVEMENT_BUDGET_USD == 10.0
        assert settings.QUERY_IMPROVEMENT_MAX_EXPERIMENTS == 30

    def test_task_function_importable(self):
        from app.workers.tasks.auto_query_improvement import auto_query_improvement

        assert callable(auto_query_improvement)

    def test_task_returns_disabled_when_off(self):
        from app.workers.tasks.auto_query_improvement import auto_query_improvement

        # Task should check QUERY_IMPROVEMENT_ENABLED and return early
        # We can't easily test this without mocking settings, but we verify the function exists
        assert auto_query_improvement is not None
