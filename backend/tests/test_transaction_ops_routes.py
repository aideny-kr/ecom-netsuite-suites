def test_framework_source_routes_are_available_on_the_application_router():
    from app.api.v1.router import api_router

    paths = {route.path for route in api_router.routes}
    assert "/api/v1/transaction-sources/celigo/steps/{step_id}/orders/{order_reference}" in paths
    assert "/api/v1/transaction-sources/celigo/steps/{step_id}/orders" in paths


def test_run_and_operation_apis_are_registered_with_model_metadata():
    from app.api.v1.router import api_router
    from app.models.base import Base

    paths = {route.path for route in api_router.routes}
    assert "/api/v1/transaction-ops/configs/{config_id}/runs" in paths
    assert "/api/v1/transaction-ops/proposals/{proposal_id}/operation" in paths
    assert "transaction_ops_runs" in Base.metadata.tables
