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
        db, tenant_a.id, scope={"source_connection_id": "source-two", "netsuite_account_id": "123", "subsidiary_id": "2"}
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
