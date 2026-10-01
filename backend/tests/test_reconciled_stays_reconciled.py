"""A reconciled order stays reconciled (decided by the user, 2026-09-30).

Measured on Framework staging over 14 days: 610 cases reconciled and 66 reopened later. In 63 of
the 66 neither Solidus nor NetSuite had changed; a verified fix had reconciled the order on the
invoice less its credit, and the next scheduled scan compared the sales order total instead. A
later scan now never reopens a reconciled case; its reading is kept as an observation. Telling a
real later change from a re-read (and listing those orders) is a separate change.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionCase, TransactionCaseObservation
from app.schemas.transaction_runs import ProgressUpdate
from app.services.transaction_ops import case_service
from app.services.transaction_ops import state_service as state
from tests.test_transaction_finding_batch import batch_setup, publish  # noqa: F401  (fixture)


async def events(db, tenant, action):
    return list(
        await db.scalars(
            select(AuditEvent).where(AuditEvent.tenant_id == tenant, AuditEvent.action == "transaction_ops." + action)
        )
    )


async def reconcile_all(db, tenant):
    cases = list(
        await db.scalars(
            select(TransactionCase).where(TransactionCase.tenant_id == tenant).order_by(TransactionCase.order_reference)
        )
    )
    for case in cases:
        case.status = "reconciled"
    await db.flush()
    return cases


async def rearm(db, setup):
    actor, _, run, token, refs, _ = setup
    await state.update_progress(
        db,
        actor.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"pending_refs": refs, "processed": 0}),
        lease_token=token,
    )


async def test_a_later_scan_of_the_same_records_keeps_the_case_reconciled(db, batch_setup):  # noqa: F811
    actor, _, _, _, _, reports = batch_setup
    now = datetime.now(timezone.utc)
    await publish(db, batch_setup, now=now)
    cases = await reconcile_all(db, actor.tenant_id)
    proof = [deepcopy(case.latest_report_json) for case in cases]
    await rearm(db, batch_setup)
    again = deepcopy(reports)
    for report in again:
        report["reason_for_test"] = "the scheduled scan compared the sales order total"
    await publish(db, batch_setup, reports=again, now=now + timedelta(seconds=1))
    assert [case.status for case in cases] == ["reconciled"] * 3
    # The reconciling evidence stays the case's evidence; the scan's reading is kept beside it.
    assert [case.latest_report_json for case in cases] == proof
    observations = await db.scalar(
        select(func.count())
        .select_from(TransactionCaseObservation)
        .where(TransactionCaseObservation.tenant_id == actor.tenant_id)
    )
    assert observations == 6
    assert not await events(db, actor.tenant_id, "case.reopened")
    kept = await events(db, actor.tenant_id, "case.kept_reconciled")
    assert len(kept) == 3 and all(event.payload["observation_id"] for event in kept)


@pytest.mark.parametrize(
    "change",
    ["solidus_order_updated", "solidus_refunds_changed", "netsuite_record_updated", "netsuite_records_changed"],
)
async def test_a_real_later_change_never_reopens_a_reconciled_case(db, batch_setup, change):  # noqa: F811
    # Telling a real change from a re-read is a separate change; here the case must simply stay put.
    actor, _, _, _, _, reports = batch_setup
    now = datetime.now(timezone.utc)
    await publish(db, batch_setup, now=now)
    cases = await reconcile_all(db, actor.tenant_id)
    proof = [deepcopy(case.latest_report_json) for case in cases]
    await rearm(db, batch_setup)
    again = deepcopy(reports)
    later = (now + timedelta(days=3)).isoformat()
    for report in again:
        if change == "solidus_order_updated":
            report["source"]["updated_at"] = later
        elif change == "solidus_refunds_changed":
            report["balance"]["amounts"]["refunds"]["source"] = "41.38"
        elif change == "netsuite_record_updated":
            report["targets"][0]["updated_at"] = later
        else:
            report["targets"][0]["record_id"] = "999"
    await publish(db, batch_setup, reports=again, now=now + timedelta(seconds=1))
    assert [case.status for case in cases] == ["reconciled"] * 3
    assert [case.latest_report_json for case in cases] == proof
    assert not await events(db, actor.tenant_id, "case.reopened")
    kept = await events(db, actor.tenant_id, "case.kept_reconciled")
    assert len(kept) == 3 and all(event.payload["evidence_observed_at"] for event in kept)


async def test_the_single_writer_keeps_a_reconciled_case_reconciled(db, batch_setup):  # noqa: F811
    actor, _, run, token, refs, reports = batch_setup
    now = datetime.now(timezone.utc)
    await state.record_finding(db, actor.tenant_id, run.id, refs[0], reports[0], lease_token=token, now=now)
    case = await db.scalar(
        select(TransactionCase).where(
            TransactionCase.tenant_id == actor.tenant_id, TransactionCase.order_reference == refs[0]
        )
    )
    case.status = "reconciled"
    await db.flush()
    proof = deepcopy(case.latest_report_json)
    changed = deepcopy(reports[0])
    changed["source"]["updated_at"] = (now + timedelta(days=1)).isoformat()
    finding = SimpleNamespace(id=uuid4(), order_reference=refs[0], report_json=changed)
    await case_service.observe_finding(db, actor.tenant_id, run, finding, now=now + timedelta(seconds=1))
    assert case.status == "reconciled" and case.latest_report_json == proof
    assert not await events(db, actor.tenant_id, "case.reopened")
    assert len(await events(db, actor.tenant_id, "case.kept_reconciled")) == 1


async def test_an_open_case_still_takes_the_newer_evidence(db, batch_setup):  # noqa: F811
    actor, _, _, _, _, reports = batch_setup
    now = datetime.now(timezone.utc)
    await publish(db, batch_setup, now=now)
    await rearm(db, batch_setup)
    again = deepcopy(reports)
    for report in again:
        report["reason_for_test"] = "newer"
    await publish(db, batch_setup, reports=again, now=now + timedelta(seconds=1))
    cases = list(await db.scalars(select(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id)))
    assert all(case.status == "open" and case.latest_report_json["reason_for_test"] == "newer" for case in cases)
    assert not await events(db, actor.tenant_id, "case.kept_reconciled")


async def test_the_database_refuses_to_reopen_a_reconciled_case(db, batch_setup):  # noqa: F811
    actor = batch_setup[0]
    await publish(db, batch_setup)
    cases = await reconcile_all(db, actor.tenant_id)
    with pytest.raises(DBAPIError, match="reconciled_case_is_final"):
        async with db.begin_nested():
            await db.execute(update(TransactionCase).where(TransactionCase.id == cases[0].id).values(status="open"))


async def test_review_results_and_groups_show_a_reconciled_case_as_matched(db, admin_user, monkeypatch):
    from app.services.transaction_ops.case_groups import list_groups
    from app.services.transaction_ops.case_service import case_scope
    from app.services.transaction_ops.period_review import review_results
    from app.services.transaction_ops.workspace_results import review_page
    from tests.test_transaction_case_groups import seed
    from tests.test_transaction_review_results import evidence
    from tests.test_transaction_review_slices import review

    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    await seed(db, actor.tenant_id, 3, scope=case_scope(root))
    cases = list(
        await db.scalars(
            select(TransactionCase)
            .where(TransactionCase.tenant_id == actor.tenant_id)
            .order_by(TransactionCase.order_reference)
        )
    )
    for case in cases:
        row = await evidence(
            db, actor, root, case.order_reference, "difference", root.created_at, source_id=str(case.id)
        )
        report = deepcopy(case.latest_report_json)
        report.update(case_id=str(case.id), source={"record_id": str(case.id)})
        row.report_json = {**row.report_json, **report}
    cases[0].status = "reconciled"
    # The same order number reconciled under another subsidiary says nothing about this one.
    twin = uuid4()
    db.add(
        TransactionCase(
            id=twin,
            tenant_id=actor.tenant_id,
            case_key=twin.hex,
            order_reference=cases[1].order_reference,
            scope_json={**case_scope(root), "subsidiary_id": "other"},
            status="reconciled",
            first_observed_at=root.created_at,
            last_observed_at=root.created_at,
            latest_report_json=deepcopy(cases[1].latest_report_json),
        )
    )
    await db.flush()
    results = await review_results(db, actor.tenant_id, root.id, limit=5)
    assert results["summary"] == {"checked": 3, "matched": 1, "needs_review": 2, "not_verified": 0}
    page = await review_page(db, actor.tenant_id, [root.id], limit=5, status="needs_review")
    assert sorted(item["order_reference"] for item in page["items"]) == sorted(c.order_reference for c in cases[1:])
    groups = await list_groups(db, actor.tenant_id, review_run_ids=[str(root.id)], status="needs_review")
    assert sum(group["case_count"] for group in groups["groups"]) == 2


async def test_the_export_and_results_page_show_a_reconciled_order_as_reconciled(
    app, client, db, admin_user, monkeypatch
):
    # Review findings F2 and F3 (#364): the workbook and the table label rows from the raw verdict.
    from io import BytesIO

    from openpyxl import load_workbook

    from app.api.v1.transaction_ops import router
    from app.services.transaction_ops.case_service import case_scope
    from app.services.transaction_ops.workspace_results import review_page
    from tests.test_transaction_workspace_reports import fixture_rows, linked

    app.include_router(router, prefix="/api/v1")
    actor, headers = admin_user
    first, second, cases = await fixture_rows(db, actor, monkeypatch, count=2)
    identity = uuid4()
    reconciled = TransactionCase(
        id=identity,
        tenant_id=actor.tenant_id,
        case_key=identity.hex,
        order_reference="R900000001",
        scope_json=case_scope(first),
        status="reconciled",
        first_observed_at=first.created_at,
        last_observed_at=first.created_at,
        latest_report_json=deepcopy(cases[0].latest_report_json),
    )
    db.add(reconciled)
    await db.flush()
    await linked(db, actor, first, reconciled)
    page = await review_page(db, actor.tenant_id, [first.id, second.id], limit=10)
    flags = {item["order_reference"]: item["reconciled"] for item in page["items"]}
    assert flags == {"R900000001": True, cases[0].order_reference: False, cases[1].order_reference: False}
    assert page["summary"]["matched"] == 1 and page["summary"]["needs_review"] == 2
    response = await client.post(
        "/api/v1/transaction-ops/review-export",
        headers=headers,
        json={"review_run_ids": [str(first.id), str(second.id)]},
    )
    assert response.status_code == 200, response.text[:300]
    wb = load_workbook(BytesIO(response.content))
    findings = {str(row[0].value): row[4].value for row in wb["Reconciliation"].iter_rows(min_row=2)}
    [reference] = [key for key in findings if "R900000001" in key]
    assert findings[reference] == "matched"
    assert sum(row[5].value for row in wb["Issue groups"].iter_rows(min_row=2)) == 2
