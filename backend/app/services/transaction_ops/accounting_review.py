"""Deterministic accounting context; configuration presence is never write approval."""

from decimal import localcontext

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.transaction_ops import TransactionConfig
from app.schemas.transaction_ops import _decimal

SCOPE_FIELDS = ("source_connection_id", "source_step_id", "netsuite_account_id", "subsidiary_id", "record_type")


def scope_projection(value):
    result = {key: str(value[key]) if value.get(key) is not None else None for key in SCOPE_FIELDS}
    if result["netsuite_account_id"]:
        result["netsuite_account_id"] = result["netsuite_account_id"].replace("_", "-").lower()
    return result


def metric_assessment(report):
    """Preserve independently known metrics without exposing model-computed money."""
    balance = report.get("balance") or {}
    result = {}
    deltas = {}
    for metric in ("order_total", "tax", "refunds"):
        result[metric] = {"status": "not_verified", "direction": None}
        try:
            if (
                balance.get("status") not in {"matched", "difference", "incomplete"}
                or not balance.get("currency")
                or balance.get("currency") != balance.get("target_currency")
                or metric in (balance.get("missing_metrics") or [])
            ):
                continue
            values = balance["amounts"][metric]
            source, target, delta = (_decimal(values[key]) for key in ("source", "target", "delta"))
            with localcontext() as context:
                context.prec = 50
                if source - target != delta or (balance["status"] == "matched" and delta != 0):
                    continue
            deltas[metric] = delta
            result[metric] = {
                "status": "matched" if delta == 0 else "difference",
                "direction": "equal" if delta == 0 else "source_higher" if delta > 0 else "target_higher",
            }
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return {
        "metrics": result,
        "gross_difference_equals_tax_difference": (
            bool(deltas["tax"]) and deltas["order_total"] == deltas["tax"]
            if "tax" in deltas and "order_total" in deltas
            else None
        ),
        "interpretation": "Metric results describe this observation. Repair-detail blockers do not negate "
        "known differences or make matched refunds unknown. Equal gross/tax deltas do not prove root cause, "
        "tax correctness, or posted GL impact. Gross and tax deltas must not be added together.",
    }


ACCOUNTING_CHECKS = [
    {
        "check": "identity_and_environment",
        "required_evidence": "Bind each read to the scoped connection, actual NetSuite account/environment, "
        "subsidiary, "
        "order reference, native record ID/type and transaction currency. Inspect the connected tool's "
        "account binding; "
        "never infer it from a browser, generated link, default account or matching record ID in another environment. "
        "Do not combine cross-account/subsidiary/currency evidence. Stop dependent investigation on ambiguous "
        "identity. Use netsuite_suiteql with BOTH connection_id and expected_account_id from query_scope_params. "
        "Unscoped query success does not prove this account binding. Restrict SQL to the scoped subsidiary and IDs.",
    },
    {
        "check": "lifecycle_and_posted_documents",
        "required_evidence": "Read current native sales order, linked invoice/cash sale, fulfillment, credit memos, "
        "refunds, applications and relevant custom refund requests. Sales orders are non-posting: a billed SO header "
        "difference does not establish a posted ledger or invoice error. Trace actual posting documents and GL impact. "
        "Negative SuiteQL line signs alone do not establish a return, credit or reversal. Decode lifecycle using "
        "native status display/metadata. On a failed query inspect schema/permissions and try a supported alternate "
        "read; do not label an unexplained 500 transient or repeat an unchanged invalid query. "
        "Discover linked-document "
        "fields on the actual transaction/transaction-line schema rather than guessing a header createdfrom field.",
    },
    {
        "check": "tax_and_integration_configuration",
        "required_evidence": "Read the actual account tax regime (SuiteTax versus legacy/external engine), subsidiary "
        "nexus, tax codes/types/rates and effective dates, inclusive/exclusive basis, exemptions, "
        "item/shipping/discount "
        "tax allocation, rounding, tax accounts, and applicable Celigo mappings/recalculation/custom scripts. "
        "Compare finalized source evidence with native details; source tax is not automatically legally correct. "
        "An app mapping or absent legacy profile does not prove the account tax regime. SuiteTax override requires "
        "native tax-detail references, complete details and appropriate permissions; never assume legacy fields apply.",
    },
    {
        "check": "accounting_treatment",
        "required_evidence": "Establish which system/document is wrong and whether this is an operational-only, "
        "tax-reporting, AR, revenue or cash issue. Check posting period locks/close, tax filing status, "
        "accounting book, "
        "currency/exchange rate, allowed accounts/segments, roles and approval workflows. Do not default to a journal, "
        "credit/rebill, extra refund, period reopening or an SO edit just to match totals. A journal may fail "
        "to update "
        "tax reports or invoice tax; prove the relevant effects before recommending it. Missing permissions/metadata "
        "are specific blockers, not evidence that a setting is absent.",
    },
    {
        "check": "proposal_and_verification",
        "required_evidence": "Distinguish investigation advice from an executable supported proposal. Bind exact "
        "before/after fields, document IDs, accounts/tax codes, amount/currency, rationale and expected "
        "tax/AR/GL effects "
        "to fresh evidence. Require exact human approval, audited approver and execution receipt, idempotency and "
        "concurrent-change checks. Re-read native documents and re-reconcile after execution; verify payment/payout "
        "clearance separately. Bulk fixes require the same validated accounting treatment and individually "
        "bound records.",
    },
]


def observed_scope(report, scope):
    """Identify conflicts before suggesting further reads in the wrong account."""
    targets = report.get("targets") or []
    source = report.get("source") or {}
    lookup = report.get("lookup") or {}
    result = {"status": "not_verified", "target_records": []}
    if len(targets) != 1 or not isinstance(targets[0], dict):
        return result
    target = targets[0]
    result["target_records"] = [
        {
            key: target.get(key)
            for key in ("record_id", "record_type", "account_id", "subsidiary_id", "currency", "status", "observed_at")
        }
    ]
    expected = {
        "account_id": scope.get("netsuite_account_id"),
        "subsidiary_id": scope.get("subsidiary_id"),
        "record_type": scope.get("record_type"),
        "order_reference": report.get("order_reference"),
        "currency": source.get("currency"),
    }
    for key, value in expected.items():
        actual = target.get(key)
        if key == "account_id" and actual:
            actual = str(actual).replace("_", "-").lower()
        if actual is None or value is None:
            return result
        if str(actual) != str(value):
            result["status"] = "conflict"
            return result
    if source.get("subsidiary_id") != scope.get("subsidiary_id") or source.get("order_reference") != report.get(
        "order_reference"
    ):
        result["status"] = "conflict"
    elif (
        source.get("authoritative") is True
        and target.get("authoritative") is True
        and lookup.get("complete") is True
        and lookup.get("authoritative") is True
    ):
        result["status"] = "consistent_in_stored_observation"
    return result


async def accounting_context(db, tenant_id, scope, report=None):
    """Read current tenant configuration; never dump credentials or arbitrary metadata."""
    scope = scope_projection(scope)
    context = {
        "scope": scope,
        "observed_scope": observed_scope(report or {}, scope),
        "configuration_status": "scope_unavailable",
        "native_accounting_validation": "required_not_performed_by_status_read",
        "checks": ACCOUNTING_CHECKS,
        "read_only_next_step": "Continue the user's requested investigation with available scoped read tools without "
        "asking discretionary permission. Reuse recent evidence for triage; refresh changed/missing evidence and "
        "revalidate before a proposal/write. Do not rerun the same header scan to obtain native detail it "
        "cannot return. "
        "Respect an actual tool approval requirement and report the exact blocker if one occurs.",
        "supported_write_scope": "Conditional existing-line corrections on eligible unfulfilled/unbilled sales orders "
        "and mapped missing-order creation only. Posted invoice/tax journal/credit-memo/refund adjustments are not "
        "supported by this workflow. Configuration presence never proves native guard readiness or approval.",
        "references": [
            "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N1452887.html",
            "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N1219162.html",
            "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4283867856.html",
        ],
    }
    if not all(scope[key] for key in ("netsuite_account_id", "subsidiary_id", "record_type")) or not (
        bool(scope["source_connection_id"]) != bool(scope["source_step_id"])
    ):
        return context
    await set_tenant_context(db, str(tenant_id))
    configs = list(await db.scalars(select(TransactionConfig).where(TransactionConfig.tenant_id == tenant_id)))
    matches = [
        config
        for config in configs
        if config.enabled and scope_projection({key: getattr(config, key) for key in SCOPE_FIELDS}) == scope
    ]
    context["configuration_status"] = "ambiguous" if len(matches) > 1 else "unavailable"
    if len(matches) != 1:
        return context
    config = matches[0]
    mapping = config.mapping_json or {}
    connection = await db.scalar(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.id == config.netsuite_connection_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
    )
    context.update(
        configuration_status="scoped_configuration_found",
        config_id=str(config.id),
        netsuite_connection_id=str(config.netsuite_connection_id),
        query_scope_params={
            "connection_id": str(config.netsuite_connection_id),
            "expected_account_id": scope["netsuite_account_id"],
        },
        connection_active=connection is not None,
        action_mode=mapping.get("action_mode", "detect_only"),
        create_profile_configured=bool(mapping.get("netsuite_create")),
        legacy_tax_profile_configured=bool(mapping.get("netsuite_legacy_tax")),
        guard_url_configured=bool(connection and (connection.metadata_json or {}).get("transaction_ops_guard_url")),
        native_tax_regime="not_verified",
    )
    return context
