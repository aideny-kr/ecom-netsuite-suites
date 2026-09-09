"""Server pagination over the whole selected review, shared with exports."""

from uuid import UUID

from sqlalchemy import String, and_, func, literal, or_, select, union_all

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionCase, TransactionProposal, TransactionRun
from app.schemas.transaction_runs import CaseOut, ProposalOut, RunOut
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.review_evidence import period_evidence, result_category

CATEGORIES = ("matched", "needs_review", "not_verified")


async def selected_evidence(db, tenant_id, run_ids):
    if not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 20:
        raise state.StateError("invalid_review_selection", 422)
    try:
        ids = sorted({UUID(str(value)) for value in run_ids})
    except (TypeError, ValueError, AttributeError):
        raise state.StateError("invalid_review_selection", 422) from None
    queries, scopes = [], []
    for run_id in ids:
        latest, span = await period_evidence(db, tenant_id, run_id)
        run = await state.get_run(db, tenant_id, run_id)
        queries.append(
            select(
                latest,
                literal(str(run_id), String).label("review_run_id"),
                literal(str(run.config_id), String).label("config_id"),
            )
        )
        scopes.append(
            {
                "run_id": str(run_id),
                "config_id": str(run.config_id),
                "period": span.model_dump(mode="json"),
                "config": run.config_snapshot,
            }
        )
    combined = union_all(*queries).subquery()
    # Overlapping selected roots can refer to the same finding. Count it once.
    latest = select(combined).distinct(combined.c.id).order_by(combined.c.id, combined.c.review_run_id).subquery()
    return latest, scopes


def filtered_query(latest, status=None, search=""):
    if status not in (None, "", *CATEGORIES):
        raise state.StateError("invalid_result_status", 422)
    if not isinstance(search, str) or len(search) > 200:
        raise state.StateError("invalid_search", 422)
    query = select(latest)
    if status:
        query = query.where(result_category(latest) == status)
    if search:
        query = query.where(latest.c.order_reference.contains(search, autoescape=True))
    return query


def result_item(row):
    report = row["report_json"]
    return {
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "review_run_id": row["review_run_id"],
        "config_id": row["config_id"],
        "order_reference": row["order_reference"],
        "observed_at": row["updated_at"].isoformat(),
        "balance": report.get("balance"),
        "case_id": report.get("case_id"),
        "action": (report.get("comparison") or {}).get("recommended_action"),
        "automation": report.get("automation"),
    }


async def review_page(db, tenant_id, run_ids, *, limit=50, offset=0, status=None, search=""):
    latest, _ = await selected_evidence(db, tenant_id, run_ids)
    category = result_category(latest)
    summary = (
        (
            await db.execute(
                select(
                    func.count().label("checked"),
                    *[func.count().filter(category == name).label(name) for name in CATEGORIES],
                ).select_from(latest)
            )
        )
        .mappings()
        .one()
    )
    query = filtered_query(latest, status, search)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        (await db.execute(query.order_by(latest.c.order_reference, latest.c.id).offset(offset).limit(limit)))
        .mappings()
        .all()
    )
    return {
        "items": [result_item(row) for row in rows],
        "summary": dict(summary),
        "total": total,
        "has_next": offset + len(rows) < total,
    }


async def record_page(db, tenant_id, view, *, limit=50, offset=0, config_id=None):
    await set_tenant_context(db, str(tenant_id))
    model, output = {
        "cases": (TransactionCase, CaseOut),
        "runs": (TransactionRun, RunOut),
        "proposals": (TransactionProposal, ProposalOut),
    }[view]
    query = select(model).where(model.tenant_id == tenant_id)
    if view == "cases":
        query = query.where(model.status == "open")
        ordering = (model.last_observed_at.desc(), model.id)
    else:
        ordering = (model.created_at.desc(), model.id)
    if config_id is not None:
        if view != "runs":
            raise state.StateError("invalid_page_scope", 422)
        config = await state.get_config(db, tenant_id, config_id)
        scope = [
            model.config_snapshot[key].astext
            == (str(getattr(config, key)) if getattr(config, key) is not None else None)
            for key in ("source_connection_id", "source_step_id", "subsidiary_id", "record_type")
        ]
        scope.append(
            func.lower(func.replace(model.config_snapshot["netsuite_account_id"].astext, "_", "-"))
            == str(config.netsuite_account_id).replace("_", "-").lower()
        )
        query = query.where(or_(model.config_id == config_id, and_(*scope)))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(*ordering).offset(offset).limit(limit))).all()
    return {
        "items": [output.model_validate(row) for row in rows],
        "total": total,
        "has_next": offset + len(rows) < total,
    }
