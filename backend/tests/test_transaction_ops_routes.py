def test_framework_source_routes_are_available_on_the_application_router():
    from app.main import create_app

    # A fresh schema resolves included routers without relying on FastAPI internals.
    paths = create_app().openapi()["paths"]
    assert "get" in paths["/api/v1/transaction-sources/celigo/steps/{step_id}/orders/{order_reference}"]
    assert "get" in paths["/api/v1/transaction-sources/celigo/steps/{step_id}/orders"]


def test_run_and_operation_apis_are_registered_with_model_metadata():
    from app.main import create_app
    from app.models.base import Base

    paths = create_app().openapi()["paths"]
    assert "post" in paths["/api/v1/transaction-ops/configs/{config_id}/runs"]
    assert "get" in paths["/api/v1/transaction-ops/proposals/{proposal_id}/operation"]
    assert "transaction_ops_runs" in Base.metadata.tables
