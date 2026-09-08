"""Refresh a fixed review cohort without changing its historical findings."""

from sqlalchemy import func, select, true, tuple_

from app.models.transaction_ops import TransactionFinding as Finding
from app.models.transaction_ops import TransactionRun as Run


def current_review_evidence(cohort, tenant_id, snapshot):
    # A configuration revision can change mapping policy, but a different source
    # or NetSuite business entity must never supply a replacement finding.
    if not (snapshot.get("source_connection_id") or snapshot.get("source_step_id")) or not all(
        snapshot.get(key) for key in ("netsuite_account_id", "subsidiary_id", "record_type")
    ):
        return cohort
    scope = [
        Run.config_snapshot[key].astext == snapshot.get(key)
        for key in ("source_connection_id", "source_step_id", "subsidiary_id", "record_type")
    ]
    account = str(snapshot["netsuite_account_id"]).replace("_", "-").lower()
    scope.append(func.lower(func.replace(Run.config_snapshot["netsuite_account_id"].astext, "_", "-")) == account)
    replacement = (
        select(Finding.id, Finding.run_id, Finding.report_json, Finding.updated_at)
        .join(Run, (Run.id == Finding.run_id) & (Run.tenant_id == tenant_id))
        .where(
            Finding.tenant_id == tenant_id,
            Finding.order_reference == cohort.c.order_reference,
            *scope,
            # Unknown identity is not evidence that two equal references are the
            # same order. Keep the original finding when identity is incomplete.
            Finding.report_json["source"]["record_id"].astext != "",
            Finding.report_json["source"]["record_id"].astext == cohort.c.report_json["source"]["record_id"].astext,
            Finding.report_json["balance"]["currency"].astext != "",
            Finding.report_json["balance"]["currency"].astext == cohort.c.report_json["balance"]["currency"].astext,
            tuple_(Finding.updated_at, Finding.id) > tuple_(cohort.c.updated_at, cohort.c.id),
        )
        .order_by(Finding.updated_at.desc(), Finding.id.desc())
        .limit(1)
        .correlate(cohort)
        .lateral()
    )
    # Project before filtering, counting or pagination so all views agree. The
    # originating run link travels with the replacement report and amounts.
    return (
        select(
            func.coalesce(replacement.c.id, cohort.c.id).label("id"),
            func.coalesce(replacement.c.run_id, cohort.c.run_id).label("run_id"),
            cohort.c.order_reference,
            func.coalesce(replacement.c.report_json, cohort.c.report_json).label("report_json"),
            func.coalesce(replacement.c.updated_at, cohort.c.updated_at).label("updated_at"),
        )
        .select_from(cohort.outerjoin(replacement, true()))
        .subquery()
    )
