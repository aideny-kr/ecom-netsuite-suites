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
