"""Scoped historical chat corrections. Examples never carry execution authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import String, and_, cast, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionCase, TransactionFinding, TransactionRun
from app.models.user import User
from app.services.transaction_ops.accounting_recheck import report_in_scope
from app.services.transaction_ops.accounting_recovery import evidence_digest
from app.services.transaction_ops.case_service import _cleared
from app.services.transaction_ops.planner import source_fingerprint
from app.services.transaction_ops.settlement import SCOPE
from app.services.transaction_ops.state_service import business_digest


def _claim(message):
    from app.services.chat.write_confirmation_service import validate_and_extract_confirmation

    try:
        so = message.structured_output
        claim = so["accounting_execution"]
        return (
            claim
            if (
                claim["version"] == 1
                and claim["confirmation_id"] == str(message.id)
                and UUID(claim["approved_by"])
                and claim["evidence_digest"] == evidence_digest(so)
                and validate_and_extract_confirmation(so, str(message.session_id))[0]
            )
            else None
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def verified_resolution(message, run, finding, case):
    """Re-establish the complete chain, not merely the stored success label."""
    try:
        so = message.structured_output
        p = so["accounting_review"]
        claim = _claim(message)
        result = run.progress_json["settlement"]
        checked = datetime.fromisoformat(result["checked_at"])
        report = finding.report_json
        current = case.latest_report_json
        current_observed = max(
            datetime.fromisoformat(s["observed_at"]) for s in [current["source"], *current["targets"]]
        )
        return bool(
            claim
            and checked.utcoffset() is not None
            and message.role == "assistant"
            and message.tenant_id == run.tenant_id == finding.tenant_id == case.tenant_id
            and p["tenant_id"] == str(message.tenant_id)
            and so["status"] == "approved"
            and so["accounting_verification"]["status"] == "verified"
            and p.get("kind") in {"sales_adjustment_credit", "invoice_sales_adjustment"}
            and p["scope"] == case.scope_json
            and p["case_id"] == str(case.id)
            and p["order_reference"] == case.order_reference == finding.order_reference
            and p["config_id"] == str(run.config_id)
            and run.work_key == business_digest({"accounting_recheck_confirmation": str(message.id)})
            and run.origin == "recovery"
            and run.status == "finished"
            and run.termination_reason == "done"
            and run.params_json["approval_message_id"] == str(message.id)
            and run.params_json["verification_scope"] == SCOPE
            and run.params_json["order_references"] == [p["order_reference"]]
            and run.params_json["approved_by"] == claim["approved_by"] == str(run.initiated_by)
            and result["approval_message_id"] == str(message.id)
            and result["approved_by"] == claim["approved_by"]
            and result["case_id"] == str(case.id)
            and result["finding_id"] == str(finding.id)
            and result["status"] == "succeeded"
            and result["verification_scope"] == SCOPE
            and result["termination_reason"] == "done"
            and finding.run_id == run.id
            and case.status == "reconciled"
            and report_in_scope(run, p, report, checked)
            and _cleared(report, checked)
            and _cleared(current, current_observed)
            and source_fingerprint(current["source"]) == source_fingerprint(report["source"])
            and source_fingerprint(current["targets"][0]) == source_fingerprint(report["targets"][0])
            and current["balance"] == report["balance"]
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return False


def _query(tenant_id, case):
    so = cast(ChatMessage.structured_output, JSONB)
    p = so["accounting_review"]
    return (
        select(ChatMessage, TransactionRun, TransactionFinding, TransactionCase, User.full_name)
        .outerjoin(
            TransactionRun,
            and_(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.params_json["approval_message_id"].astext == cast(ChatMessage.id, String),
            ),
        )
        .outerjoin(
            TransactionFinding,
            and_(
                TransactionFinding.tenant_id == tenant_id,
                TransactionFinding.run_id == TransactionRun.id,
                TransactionFinding.order_reference == p["order_reference"].astext,
            ),
        )
        .outerjoin(
            TransactionCase,
            and_(
                TransactionCase.tenant_id == tenant_id,
                cast(TransactionCase.id, String) == p["case_id"].astext,
            ),
        )
        .outerjoin(
            User,
            and_(
                User.tenant_id == tenant_id,
                cast(User.id, String) == so["accounting_execution"]["approved_by"].astext,
            ),
        )
        .where(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.role == "assistant",
            p["tenant_id"].astext == str(tenant_id),
            p["scope"] == case.scope_json,
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )


def _project(record):
    message, run, finding, case, name = record
    so = message.structured_output or {}
    p = so.get("accounting_review") or {}
    claim = _claim(message)
    result = (run.progress_json or {}).get("settlement") or {} if run else {}
    assessment = p.get("resolution_assessment") or {}
    return {
        "origin": "chat_accounting",
        "approval_message_id": str(message.id),
        "case_id": p.get("case_id"),
        "order_reference": p.get("order_reference"),
        "treatment": p.get("kind") or "invoice_tax",
        "status": so.get("status"),
        "approved_by": claim["approved_by"] if claim else None,
        "approved_by_name": name if claim else None,
        "approved_at": claim.get("accepted_at") if claim else None,
        "native_verification": (so.get("accounting_verification") or {}).get("status", "not_verified"),
        "reconciliation_status": result.get("status", "not_evaluated"),
        "reconciliation_run_id": str(run.id) if run else None,
        "verification_finding_id": str(finding.id) if finding else None,
        "verified_example": verified_resolution(message, run, finding, case),
        "assessment_fingerprint": assessment.get("assessment_fingerprint"),
        "evidence_fingerprint": claim.get("evidence_digest") if claim else None,
        "rationale": str(p.get("approval_basis") or "")[:750],
        "configuration_basis": {
            k: p.get(k)
            for k in ("scope", "accounting_book", "ar_account", "sales_adjustment_account", "tax_account", "period")
        },
        "profile_fingerprint": business_digest(p.get("profile")) if p.get("profile") else None,
        "reference_audit_ids": [r.get("audit_id") for r in assessment.get("references", [])[:3]],
        "requires_new_human_approval": True,
        "cash_settlement": "not_verified",
        "approval_url": f"/api/v1/chat/sessions/{message.session_id}",
    }


async def history(db, tenant_id, case, current_run, *, limit=3, offset=0):
    from app.services.transaction_ops.resolution_history import _issue
    from app.services.transaction_ops.state_service import StateError

    if case.tenant_id != tenant_id or (current_run and current_run.tenant_id != tenant_id):
        raise StateError("accounting_history_scope_mismatch")
    limit, offset = min(25, max(1, limit)), max(0, offset)
    so = cast(ChatMessage.structured_output, JSONB)
    p = so["accounting_review"]
    rows = (
        await db.execute(
            _query(tenant_id, case).where(p["case_id"].astext == str(case.id)).offset(offset).limit(limit + 1)
        )
    ).all()
    candidates = []
    signature = _issue(case.latest_report_json)
    if current_run and signature and signature[1] == "difference":
        from app.services.transaction_ops.accounting_profiles import sales_credit_profile
        from app.services.transaction_ops.state_service import get_config

        config = await get_config(db, tenant_id, current_run.config_id)
        try:
            profile = await sales_credit_profile(db, tenant_id, config) if config.enabled else None
        except ValueError:
            profile = None
        if profile:
            candidates = (
                await db.execute(
                    _query(tenant_id, case)
                    .where(
                        p["case_id"].astext != str(case.id),
                        p["profile"] == profile,
                        p["resolution_assessment"]["comparison_signature"] == signature,
                        p["connection_id"].astext == str(config.netsuite_connection_id),
                        so["status"].astext == "approved",
                        TransactionCase.status == "reconciled",
                        TransactionRun.config_snapshot["mapping_json"]
                        == current_run.config_snapshot.get("mapping_json"),
                        TransactionRun.status == "finished",
                        TransactionRun.termination_reason == "done",
                        TransactionRun.progress_json["settlement"]["status"].astext == "succeeded",
                    )
                    .limit(26)
                )
            ).all()
    examples = [entry for record in candidates[:25] if (entry := _project(record))["verified_example"]]
    return {
        "resolutions": [_project(record) for record in rows[:limit]],
        "truncated": len(rows) > limit,
        "next_offset": offset + limit if len(rows) > limit else None,
        "examples": examples[:5],
        "examples_truncated": len(candidates) > 25 or len(examples) > 5,
        "usage": "Historical verified examples only. Revalidate current scope, profile, book, accounts, period, "
        "amounts and applications; obtain a new exact approval. No replay payload or financial policy is granted.",
    }
