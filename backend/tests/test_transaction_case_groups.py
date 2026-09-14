from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionCase
from app.services.transaction_ops.case_groups import group_members, list_groups
from app.services.transaction_ops.state_service import StateError


async def seed(db, tenant_id, count=1, *, currency="EUR", tax="0.01", scope=None, credit=False, status="open"):
    await set_tenant_context(db, str(tenant_id))
    now = datetime.now(timezone.utc)
    body = {
        "balance": {
            "status": "difference",
            "currency": currency,
            "target_currency": currency,
            "missing_metrics": [],
            "amounts": {key: {"delta": tax if key == "tax" else "0.00"} for key in ("order_total", "tax", "refunds")},
            "adjustments": [{"kind": "tax_reversal"}] if credit else [],
        },
        "targets": [{"status": "fulfilled"}],
    }
    for _ in range(count):
        identity = uuid4()
        db.add(
            TransactionCase(
                id=identity,
                tenant_id=tenant_id,
                case_key=identity.hex,
                order_reference="R" + str(identity.int)[:9],
                scope_json=scope
                or {"source_connection_id": "source-one", "netsuite_account_id": "123", "subsidiary_id": "2"},
                status=status,
                first_observed_at=now,
                last_observed_at=now,
                latest_report_json=deepcopy(body),
            )
        )
    await db.flush()


async def test_groups_cover_cases_beyond_first_page_and_members_paginate(db, tenant_a):
    await seed(db, tenant_a.id, 53)
    result = await list_groups(db, tenant_a.id, limit=1)
    assert len(result["groups"]) == 1 and not result["has_next"]
    group = result["groups"][0]
    assert group["case_count"] == 53 and group["pattern"] == "Tax differences"
    assert group["cause_verified"] is False
    first = await group_members(db, tenant_a.id, group["group_id"])
    second = await group_members(db, tenant_a.id, group["group_id"], offset=50)
    assert len(first["cases"]) == 50 and first["has_next"]
    assert len(second["cases"]) == 3 and not second["has_next"]
    assert not {x["case_id"] for x in first["cases"]} & {x["case_id"] for x in second["cases"]}


async def test_grouping_separates_currency_direction_scope_and_credit_context(db, tenant_a):
    await seed(db, tenant_a.id)
    await seed(db, tenant_a.id, tax="0.02")  # Same symptom, no cause assertion.
    await seed(db, tenant_a.id, currency="USD")
    await seed(db, tenant_a.id, tax="-0.01")
    await seed(db, tenant_a.id, credit=True)
    await seed(
        db,
        tenant_a.id,
        scope={"source_connection_id": "source-two", "netsuite_account_id": "123", "subsidiary_id": "2"},
    )
    await seed(db, tenant_a.id, status="reconciled")
    result = await list_groups(db, tenant_a.id, limit=2)
    assert result["has_next"] and result["groups"][0]["case_count"] == 2
    remaining = await list_groups(db, tenant_a.id, offset=2)
    groups = result["groups"] + remaining["groups"]
    assert len(groups) == 5 and sum(g["case_count"] for g in groups) == 6
    assert "Tax differences after credits" in [g["pattern"] for g in groups]


async def test_foreign_group_ids_never_return_other_tenant_cases(db, tenant_a, tenant_b):
    await seed(db, tenant_a.id)
    group = (await list_groups(db, tenant_a.id))["groups"][0]
    assert not (await list_groups(db, tenant_b.id))["groups"]
    assert not (await group_members(db, tenant_b.id, group["group_id"]))["cases"]
    with pytest.raises(StateError, match="invalid_group_id"):
        await group_members(db, tenant_a.id, "' OR 1=1")


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "bad", 0.01, True])
async def test_malformed_delta_is_incomplete_not_rounding(db, tenant_a, value):
    await seed(db, tenant_a.id, tax=value)
    group = (await list_groups(db, tenant_a.id))["groups"][0]
    assert group["tax"] == "unknown" and group["pattern"] == "Incomplete comparison evidence"


async def test_zero_exponent_and_negative_zero_are_not_variances(db, tenant_a):
    await seed(db, tenant_a.id, tax="-0.00")
    await seed(db, tenant_a.id, tax="0E-12")
    group = (await list_groups(db, tenant_a.id))["groups"][0]
    assert group["tax"] == "zero" and group["case_count"] == 2


async def test_actual_governed_group_tool_dispatch_keeps_members_scoped(db, admin_user, admin_user_b, monkeypatch):
    import json

    from app.services.chat.tools import execute_tool_call
    from tests.conftest import enable_feature_flag

    actor, foreign = admin_user[0], admin_user_b[0]
    for user in (actor, foreign):
        for flag in ("celigo", "reconciliation"):
            await enable_feature_flag(db, user.tenant_id, flag)
    await seed(db, actor.tenant_id)
    monkeypatch.setattr("app.mcp.governance.check_rate_limit", lambda *args: True)

    async def read(user, params):
        return json.loads(
            await execute_tool_call("transaction_ops_groups", params, user.tenant_id, user.id, "group-read-test", db)
        )

    groups = await read(actor, {"limit": 1})
    assert groups.get("success") is True, groups
    group = groups["groups"][0]
    members = await read(actor, {"group_id": group["group_id"], "limit": 1})
    assert members["success"] is True and len(members["cases"]) == 1, members
    assert not (await read(foreign, {"group_id": group["group_id"]}))["cases"]


async def test_period_groups_match_review_counts_and_keep_agent_members_in_scope(
    db, admin_user, admin_user_b, monkeypatch
):
    import json
    from datetime import timedelta

    from sqlalchemy import select

    from app.services.chat.tools import execute_tool_call
    from app.services.transaction_ops.period_review import review_results
    from tests.test_transaction_review_results import evidence, recheck
    from tests.test_transaction_review_slices import review

    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    await seed(db, actor.tenant_id, 29)
    cases = list((await db.scalars(select(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id))).all())
    # 26 findings in the period, two later rechecks, three historical-only cases.
    for index, item in enumerate(cases[:26]):
        row = await evidence(
            db, actor, root, item.order_reference, "difference", root.created_at, source_id=str(item.id)
        )
        report = deepcopy(item.latest_report_json)
        report.update(case_id=str(item.id), source={"record_id": str(item.id)})
        row.report_json = report
        row.updated_at = root.created_at + timedelta(microseconds=1)
    later = await recheck(db, actor, root)
    for index, status in enumerate(("matched", "incomplete")):
        item = cases[index]
        row = await evidence(
            db,
            actor,
            later,
            item.order_reference,
            status,
            root.created_at + timedelta(seconds=1),
            source_id=str(item.id),
        )
        row.report_json = {
            **row.report_json,
            "case_id": str(item.id),
            "balance": {**row.report_json["balance"], "currency": "EUR"},
        }
        row.updated_at = root.created_at + timedelta(seconds=2)
    await db.flush()
    scope = {"review_run_ids": [str(root.id)], "status": "needs_review"}
    results = await review_results(db, actor.tenant_id, root.id, limit=1)
    assert results["summary"] == {"checked": 26, "matched": 1, "needs_review": 24, "not_verified": 1}
    groups = await list_groups(db, actor.tenant_id, **scope)
    assert sum(g["case_count"] for g in groups["groups"]) == 24
    assert len(groups["groups"]) == 1
    group_id = groups["groups"][0]["group_id"]
    members = await group_members(db, actor.tenant_id, group_id, **scope)
    assert {x["case_id"] for x in members["cases"]} == {str(c.id) for c in cases[2:26]}
    assert not (await group_members(db, actor.tenant_id, group_id))["cases"]  # Dropping scope cannot widen it.
    searched = await list_groups(db, actor.tenant_id, **scope, search=cases[2].order_reference)
    assert searched["groups"][0]["case_count"] == 1
    assert not (await group_members(db, actor.tenant_id, group_id, **scope, search=cases[2].order_reference))["cases"]
    assert (
        sum(
            g["case_count"]
            for g in (await list_groups(db, actor.tenant_id, review_run_ids=[root.id, root.id]))["groups"]
        )
        == 24
    )
    assert (
        sum(
            g["case_count"]
            for g in (await list_groups(db, actor.tenant_id, review_run_ids=[root.id], status="not_verified"))["groups"]
        )
        == 1
    )
    monkeypatch.setattr("app.mcp.governance.check_rate_limit", lambda *args: True)
    tool = json.loads(
        await execute_tool_call(
            "transaction_ops_groups",
            {**scope, "group_id": group_id},
            actor.tenant_id,
            actor.id,
            "scoped-group-test",
            db,
        )
    )
    assert tool["success"] and len(tool["cases"]) == 20 and tool["has_next"], tool
    with pytest.raises(StateError, match="not_found"):
        await list_groups(db, admin_user_b[0].tenant_id, **scope)


@pytest.mark.parametrize(
    "scope",
    [
        {"review_run_ids": []},
        {"review_run_ids": ["invalid"]},
        {"review_run_ids": [str(uuid4())] * 21},
        {"status": "needs_review"},
        {"search": "x"},
        {"search": "x" * 201},
        {"status": "approved"},
    ],
)
async def test_invalid_group_scope_does_not_fall_back_to_history(db, tenant_a, scope):
    with pytest.raises(StateError):
        await list_groups(db, tenant_a.id, **scope)
