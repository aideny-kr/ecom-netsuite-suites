import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import accounting_retry as retry
from app.services.transaction_ops.accounting_recovery import execution_claim
from app.services.transaction_ops.resolution_plan import operation_identity, previous_execution
from tests.test_accounting_approval_flow import kind_proposal


def rejected(tenant_id=None):
    p = kind_proposal("api_credit")
    p["tenant_id"] = str(tenant_id or uuid4())
    p["support"]["credit"]["lastModifiedDate"] = "2026-09-08T19:34:00Z"
    mid, actor = uuid4(), uuid4()
    so = execution_claim(
        {"accounting_review": p, "mutation_type": "update", "tool_name": "mcp", "tool_input": {}},
        mid,
        actor,
        {},
        now=datetime.now(timezone.utc),
    )
    so.update(
        status="failed",
        error="HTTP 400: "
        + json.dumps(
            {"status": 400, "o:errorDetails": [{"o:errorCode": "INVALID_VALUE", "detail": "numeric field rejected"}]}
        ),
    )
    return SimpleNamespace(id=mid, structured_output=so), deepcopy(p)


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "timeout",
        "generic",
        "malformed",
        "error_code",
        "status",
        "receipt",
        "verification",
        "digest",
        "revision_missing",
        "revision",
        "ledger",
        "source",
        "tenant",
        "scope",
        "transport",
        "read_failure",
    ],
)
async def test_rejection_requires_bound_unchanged_fresh_evidence(monkeypatch, drift):
    m, p = rejected()
    so = m.structured_output
    current = deepcopy(p["support"])
    if drift == "timeout":
        so["error"] = "timeout"
    elif drift == "generic":
        so["error"] = "failed"
    elif drift == "malformed":
        so["error"] = "HTTP 400: null"
    elif drift == "error_code":
        so["error"] = 'HTTP 400: {"status":400,"o:errorDetails":[{"o:errorCode":"OTHER"}]}'
    elif drift == "status":
        so["status"] = "indeterminate"
    elif drift == "receipt":
        so["accounting_execution"]["receipt"] = {"recordId": p["record_id"]}
    elif drift == "verification":
        so["accounting_verification"] = {"status": "verified"}
    elif drift == "digest":
        so["accounting_execution"]["evidence_digest"] = "tampered"
    elif drift == "revision_missing":
        so["accounting_review"]["support"]["credit"].pop("lastModifiedDate")
    elif drift == "revision":
        current["credit"]["lastModifiedDate"] = "later"
    elif drift == "ledger":
        current["credit_gl"] = {"changed": True}
    elif drift == "source":
        p["source"]["total"] = "999"
    elif drift == "tenant":
        p["tenant_id"] = str(uuid4())
    elif drift == "scope":
        p["scope"]["subsidiary_id"] = "99"
    elif drift == "transport":
        p["execution_transport"] = "other"
    fresh = AsyncMock(return_value=(p["source"], {}, {}, current))
    if drift == "read_failure":
        fresh.side_effect = TimeoutError()
    monkeypatch.setattr("app.services.transaction_ops.credit_api_correction.fresh", fresh)
    proof = await retry.rejected_credit_unchanged(AsyncMock(), so["accounting_review"]["tenant_id"], m, p)
    assert bool(proof) == (drift is None)
    if proof:
        assert proof["financial_writes"] == 0
        assert proof["new_human_approval_required"] is True
        assert so["status"] == "failed", "Never reset the original approval"


@pytest.mark.parametrize("older_unknown", [False, True])
async def test_duplicate_gate_releases_only_proven_rejection_and_keeps_older_unknown(
    db, admin_user, monkeypatch, older_unknown
):
    from sqlalchemy import select

    from app.models.audit import AuditEvent
    from app.models.chat import ChatMessage, ChatSession

    actor = admin_user[0]
    m, p = rejected(actor.tenant_id)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id)
    db.add(session)
    await db.flush()
    if older_unknown:
        unknown = ChatMessage(
            tenant_id=actor.tenant_id,
            session_id=session.id,
            role="assistant",
            content="",
            structured_output={
                "status": "indeterminate",
                "accounting_execution": {"operation_key": operation_identity(p)},
            },
        )
        db.add(unknown)
        await db.flush()
    old = ChatMessage(
        id=m.id,
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output=m.structured_output,
    )
    db.add(old)
    await db.flush()
    monkeypatch.setattr(
        "app.services.transaction_ops.credit_api_correction.fresh",
        AsyncMock(return_value=(p["source"], {}, {}, deepcopy(p["support"]))),
    )
    result = await previous_execution(db, actor.tenant_id, uuid4(), p)
    assert bool(result) == older_unknown
    if result:
        assert result["confirmation_id"] == str(unknown.id)
    await db.flush()
    if not older_unknown:
        audit = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.resource_id == str(old.id),
                AuditEvent.action == "accounting_correction.rejection_verified_unchanged",
            )
        )
        assert audit.payload["financial_writes"] == 0
        assert audit.payload["operation_key"] == operation_identity(p)
    assert old.structured_output["status"] == "failed"
