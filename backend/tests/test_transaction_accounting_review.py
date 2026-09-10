"""Regression for confirmed tax variances misread as unknown refunds in agent chat."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.mcp.tools.transaction_ops_tools import _finding_summary
from app.models.connection import Connection
from app.services.transaction_ops.accounting_review import (
    SCOPE_FIELDS,
    accounting_context,
    metric_assessment,
    observed_scope,
    scope_projection,
)
from app.services.transaction_ops.chat_evidence import condense_status
from tests.test_transaction_resolution_guidance import report


def evidence():
    result = report(gross=("1000.00", "984.02", "15.98"), tax=("100.00", "84.02", "15.98"))
    result.update(
        order_reference="R123456789",
        comparison={
            "recommended_action": "gather_evidence",
            "findings": [{"code": "incomplete_detail"}, {"code": "tax_calculation_unproven"}],
        },
    )
    result["balance"].update(
        source_observed_at="2026-09-10T00:52:06+00:00", target_observed_at="2026-09-10T00:52:09+00:00"
    )
    return result


def test_agent_receives_known_metrics_despite_repair_blockers_without_recomputing_money():
    body = evidence()
    before = deepcopy(body)
    summary = _finding_summary(SimpleNamespace(report_json=body))
    context = json.loads(condense_status({"findings": [summary]}))
    metrics = context["findings"][0]["reconciliation"]
    assert metrics["metrics"] == {
        "order_total": {"status": "difference", "direction": "source_higher"},
        "tax": {"status": "difference", "direction": "source_higher"},
        "refunds": {"status": "matched", "direction": "equal"},
    }
    assert metrics["gross_difference_equals_tax_difference"] is True
    assert summary["recommended_action"] == "gather_evidence"
    assert summary["observed_at"]["target"] == "2026-09-10T00:52:09+00:00"
    assert "15.98" not in json.dumps(context)  # exact amounts stay in deterministic rendered table
    assert body == before


@pytest.mark.parametrize("bad", [None, True, 15.98, "NaN", "Infinity", "1e25", "15.97"])
def test_invalid_tax_delta_never_claims_verified_tax_or_erases_known_refunds(bad):
    body = evidence()
    body["balance"]["amounts"]["tax"]["delta"] = bad
    result = metric_assessment(body)
    assert result["metrics"]["tax"]["status"] == "not_verified"
    assert result["metrics"]["refunds"]["status"] == "matched"
    assert result["gross_difference_equals_tax_difference"] is None


def test_missing_metric_currency_conflict_and_matched_contradiction_stay_unverified():
    body = evidence()
    body["balance"]["missing_metrics"] = ["refunds"]
    assert metric_assessment(body)["metrics"]["refunds"]["status"] == "not_verified"
    assert metric_assessment(body)["metrics"]["tax"]["status"] == "difference"
    body["balance"]["target_currency"] = "USD"
    assert all(m["status"] == "not_verified" for m in metric_assessment(body)["metrics"].values())
    body = evidence()
    body["balance"]["status"] = "matched"
    assert metric_assessment(body)["metrics"]["tax"]["status"] == "not_verified"


def test_adjusted_credit_residual_and_pennies_remain_separate():
    body = report(gross=("2046.68", "2046.68", "0"), tax=("0", "0.02", "-0.02"), refunds=("409.32", "409.32", "0"))
    result = metric_assessment(body)
    assert result["metrics"]["tax"] == {"status": "difference", "direction": "target_higher"}
    assert result["metrics"]["order_total"]["status"] == "matched"
    assert result["metrics"]["refunds"]["status"] == "matched"
    assert result["gross_difference_equals_tax_difference"] is False


def test_observed_scope_checks_environment_currency_and_identity_without_inferring_gl():
    scope = {"netsuite_account_id": "123-sb1", "subsidiary_id": "1", "record_type": "salesorder"}
    body = {
        "order_reference": "R123",
        "source": {"subsidiary_id": "1", "order_reference": "R123", "currency": "USD", "authoritative": True},
        "lookup": {"complete": True, "authoritative": True},
        "targets": [
            {
                "record_id": "200",
                "account_id": "123_SB1",
                "subsidiary_id": "1",
                "record_type": "salesorder",
                "order_reference": "R123",
                "currency": "USD",
                "authoritative": True,
            }
        ],
    }
    assert observed_scope(body, scope)["status"] == "consistent_in_stored_observation"
    for key, wrong in (
        ("account_id", "123"),
        ("subsidiary_id", "2"),
        ("currency", "EUR"),
        ("order_reference", "R999"),
        ("record_type", "invoice"),
    ):
        changed = deepcopy(body)
        changed["targets"][0][key] = wrong
        assert observed_scope(changed, scope)["status"] == "conflict"
    body["targets"][0]["authoritative"] = False
    assert observed_scope(body, scope)["status"] == "not_verified"


async def test_current_configuration_is_scoped_and_does_not_leak_secrets_or_assert_tax_regime(
    db, admin_user, admin_user_b
):
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    scope = {key: getattr(config, key) for key in SCOPE_FIELDS}
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.metadata_json = {"transaction_ops_guard_url": "https://private-guard", "secret": "never-expose"}
    await db.flush()
    context = await accounting_context(db, actor.tenant_id, scope)
    assert context["configuration_status"] == "scoped_configuration_found"
    assert context["config_id"] == str(config.id)
    assert context["connection_active"] is True
    assert context["guard_url_configured"] is True
    assert context["native_tax_regime"] == "not_verified"
    assert context["native_accounting_validation"] == "required_not_performed_by_status_read"
    assert "private-guard" not in json.dumps(context) and "never-expose" not in json.dumps(context)
    assert json.loads(condense_status({"accounting_review": context}))["accounting_review"] == context
    foreign = await accounting_context(db, admin_user_b[0].tenant_id, scope)
    assert foreign["configuration_status"] == "unavailable"
    assert "config_id" not in foreign and "netsuite_connection_id" not in foreign
    assert scope_projection(scope)["netsuite_account_id"] == str(config.netsuite_account_id).replace("_", "-").lower()
