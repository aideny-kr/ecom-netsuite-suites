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


async def period_evidence(db, tenant_id, run_id):
    """One authorized period's cohort with compatible later rechecks."""
    from app.models.transaction_ops import TransactionFinding, TransactionRun
    from app.schemas.transaction_runs import ReviewSpan
    from app.services.transaction_ops import state_service as state

    root = await state.get_run(db, tenant_id, run_id)
    if not root.params_json.get("review"):
        raise state.StateError("not_a_period_review", 422)
    span = ReviewSpan.model_validate(root.params_json["review"])
    f, r = TransactionFinding, TransactionRun
    cohort = (
        select(f.id, f.run_id, f.order_reference, f.report_json, f.updated_at)
        .join(r, (f.tenant_id == r.tenant_id) & (f.run_id == r.id))
        .where(
            f.tenant_id == tenant_id,
            r.tenant_id == tenant_id,
            r.config_id == root.config_id,
            r.params_json["review"] == span.model_dump(mode="json"),
        )
        .distinct(f.order_reference)
        .order_by(f.order_reference, f.updated_at.desc(), f.id.desc())
        .subquery()
    )
    latest = current_review_evidence(cohort, tenant_id, root.config_snapshot)
    return latest, span


def result_category(latest):
    from sqlalchemy import case

    verdict = latest.c.report_json["balance"]["status"].astext
    return case(
        (verdict == "matched", "matched"),
        (
            verdict.in_(["difference", "mismatch", "missing_in_netsuite", "ambiguous", "currency_mismatch"]),
            "needs_review",
        ),
        else_="not_verified",
    )
