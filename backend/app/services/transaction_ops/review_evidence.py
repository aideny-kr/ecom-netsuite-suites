"""Refresh a fixed review cohort without changing its historical findings."""

from sqlalchemy import and_, func, or_, select, tuple_

from app.models.transaction_ops import TransactionFinding as Finding
from app.models.transaction_ops import TransactionRun as Run
from app.services.transaction_ops.source_eligibility import eligible_reports


def current_review_evidence(cohort, tenant_id, snapshot):
    # A configuration revision may change policy, but a different source,
    # account, entity, identity or currency cannot replace a finding.
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
    # Materialize only identity and winner keys. A per-order correlated lookup
    # otherwise rescans the tenant's finding history thousands of times.
    identities = (
        select(
            cohort.c.id,
            cohort.c.order_reference,
            cohort.c.updated_at,
            cohort.c.report_json["source"]["record_id"].astext.label("source_id"),
            cohort.c.report_json["balance"]["currency"].astext.label("currency"),
        )
        .cte(nesting=True)
        .prefix_with("MATERIALIZED")
    )
    candidates = (
        select(identities.c.id.label("cohort_id"), Finding.id.label("finding_id"))
        .select_from(identities)
        .join(Finding, (Finding.tenant_id == tenant_id) & (Finding.order_reference == identities.c.order_reference))
        .join(Run, (Run.id == Finding.run_id) & (Run.tenant_id == tenant_id))
        .where(
            *scope,
            Finding.report_json["source"]["record_id"].astext != "",
            Finding.report_json["source"]["record_id"].astext == identities.c.source_id,
            Finding.report_json["balance"]["currency"].astext != "",
            Finding.report_json["balance"]["currency"].astext == identities.c.currency,
            tuple_(Finding.updated_at, Finding.id) > tuple_(identities.c.updated_at, identities.c.id),
        )
        .distinct(identities.c.id)
        .order_by(identities.c.id, Finding.updated_at.desc(), Finding.id.desc())
        .subquery()
    )
    winners = (
        select(
            func.coalesce(candidates.c.finding_id, identities.c.id).label("id"),
            identities.c.order_reference,
        )
        .select_from(identities.outerjoin(candidates, candidates.c.cohort_id == identities.c.id))
        .subquery()
    )
    # Hydrate evidence only after selecting the latest compatible IDs. Keep the
    # actual finding/run links, exact amounts and observation timestamps intact.
    return (
        select(
            Finding.id,
            Finding.run_id,
            winners.c.order_reference,
            Finding.report_json,
            Finding.updated_at,
        )
        .join(winners, (Finding.id == winners.c.id) & (Finding.tenant_id == tenant_id))
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
    from app.services.transaction_ops.daily_evidence import compatible_daily_runs

    f, r = TransactionFinding, TransactionRun
    cohort = (
        select(f.id, f.run_id, f.order_reference, f.updated_at)
        .join(r, (f.tenant_id == r.tenant_id) & (f.run_id == r.id))
        .where(
            f.tenant_id == tenant_id,
            r.tenant_id == tenant_id,
            r.config_id == root.config_id,
            or_(
                r.params_json["review"] == span.model_dump(mode="json"),
                and_(*compatible_daily_runs(root, span)),
            ),
        )
        .distinct(f.order_reference)
        .order_by(f.order_reference, f.updated_at.desc(), f.id.desc())
        .subquery()
    )
    # Deduplicate narrow IDs before loading potentially large evidence blobs.
    cohort = select(cohort, f.report_json).join(f, (f.id == cohort.c.id) & (f.tenant_id == tenant_id)).subquery()
    latest = current_review_evidence(cohort, tenant_id, root.config_snapshot)
    # Filter after winner selection: never resurrect a superseded exception.
    return select(latest).where(eligible_reports(latest.c.report_json)).subquery(), span


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
