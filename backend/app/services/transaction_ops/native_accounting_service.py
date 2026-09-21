"""Profile-bound preparation and independent verification of native amendments.

No generic agent can call a raw apply endpoint. The separate signed dispatcher
is the only send path; this service performs bounded evidence reads.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.services.transaction_ops import native_accounting_protocol as protocol
from app.services.transaction_ops import native_accounting_transport as transport
from app.services.transaction_ops.accounting_preview import for_intent
from app.services.transaction_ops.native_accounting_profile import get_profile
from app.services.transaction_ops.resolution_plan import fingerprint

TOOL = "transaction_ops_accounting_amendment_apply"


def _version(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("native_record_version_unverified")
    return parsed


def _same_observation(proposal, snapshot):
    before = proposal["before"]
    if _version(before.get("lastModifiedDate")) != _version(snapshot["body"].get("lastmodifieddate")):
        raise ValueError("native_record_revision_changed")
    reference = snapshot["body"].get(proposal["native_profile"]["fields"]["order_reference"])
    if (proposal["record_type"] == "creditmemo" and reference != proposal["order_reference"]) or (
        reference is not None and reference != proposal["order_reference"]
    ):
        raise ValueError("native_order_reference_changed")
    detail = before.get("line_evidence") or {}
    old_lines, native_lines = detail.get("lines"), snapshot["lines"]
    if detail.get("complete") is not True or not isinstance(old_lines, list) or len(old_lines) != len(native_lines):
        raise ValueError("native_line_observation_incomplete")
    current = {line["lineuniquekey"]: line for line in native_lines}
    if len(current) != len(native_lines) or len({str(line.get("lineUniqueKey")) for line in old_lines}) != len(
        old_lines
    ):
        raise ValueError("native_line_observation_ambiguous")
    names = {
        "line": "line",
        "lineUniqueKey": "lineuniquekey",
        "item": "item",
        "itemType": "itemtype",
        "quantity": "quantity",
        "rate": "rate",
        "amount": "amount",
        "quantityBilled": "quantitybilled",
        "quantityFulfilled": "quantityfulfilled",
        "isClosed": "isclosed",
        "isTaxable": "istaxable",
    }
    names.update({v: v for k, v in proposal["native_profile"]["fields"].items() if k != "order_reference"})
    numeric = {
        "quantity",
        "rate",
        "amount",
        "quantityBilled",
        "quantityFulfilled",
        proposal["native_profile"]["fields"]["vat_amount"],
    }
    for old in old_lines:
        row = current.get(str(old.get("lineUniqueKey")))
        if row is None or any(
            old.get(k) is None for k in ("line", "lineUniqueKey", "item", "quantity", "rate", "amount")
        ):
            raise ValueError("native_line_observation_incomplete")
        for rest, native in names.items():
            if rest not in old:
                continue
            value = old[rest].get("id") if isinstance(old[rest], dict) else old[rest]
            actual = row.get(native)
            agrees = (
                protocol._same_field(native, actual, value)
                if rest in numeric and value is not None
                else (actual is value if isinstance(value, bool) or value is None else str(actual) == str(value))
            )
            if not agrees:
                raise ValueError("native_line_observation_changed")


def signed_input(proposal):
    return {
        "recordType": proposal["record_type"],
        "recordId": proposal["record_id"],
        "proposal_digest": fingerprint(proposal),
    }


def validate_binding(tenant_id, tool_name, params, proposal):
    if (
        tool_name != TOOL
        or not proposal
        or proposal.get("tenant_id") != str(tenant_id)
        or params != signed_input(proposal)
        or proposal.get("kind") not in protocol.AMENDMENT_RECORD_TYPES
    ):
        raise ValueError("native_signed_proposal_mismatch")
    protocol.validate_intent_profile(proposal, proposal["native_profile"])
    expected = for_intent(
        proposal, {"scope": proposal["scope"]}, proposal["before"], field_map=proposal["native_profile"]["fields"]
    )
    if expected != proposal["native_request"]:
        raise ValueError("native_proposal_request_mismatch")
    protocol.validate_preview(proposal["native_profile"], expected, proposal["native_preview"])


async def current_profile(db, tenant_id, review):
    from app.services.transaction_ops.state_service import get_config

    if not review.get("config_id"):
        return None
    config = await get_config(db, tenant_id, UUID(review["config_id"]))
    return await get_profile(db, tenant_id, config)


async def _read(db, tenant_id, proposal, action, payload):
    return await transport.request(
        db, tenant_id, proposal["connection_id"], proposal["scope"]["netsuite_account_id"], action, payload
    )


async def preview_candidate(db, tenant_id, intent, review, profile):
    """No executable card until the installed account/role and native math agree."""
    proposal = deepcopy(intent)
    proposal["before"] = deepcopy(proposal["before"])
    proposal["native_profile"] = deepcopy(profile)
    protocol.validate_intent_profile(proposal, profile)
    capabilities = await _read(db, tenant_id, proposal, "capabilities", {"subsidiaryId": profile["subsidiary_id"]})
    if not protocol.validate_capabilities(profile, capabilities)["apply_enabled"]:
        raise ValueError("native_amendment_installation_not_enabled")
    request = for_intent(proposal, review, proposal["before"], field_map=profile["fields"])
    response = await _read(db, tenant_id, proposal, "preview", {"request": request})
    protocol.validate_preview(profile, request, response)
    _same_observation(proposal, response["beforeSnapshot"])
    # Native tax calculation must be based on the same observed transaction.
    before = proposal["before"]
    snapshot = response["beforeSnapshot"]["body"]
    for native, rest in (("total", "total"), ("subtotal", "subtotal"), ("taxtotal", "taxTotal")):
        if before.get(rest) is None and rest == "taxTotal" and proposal["kind"] == "credit_tax_reallocation":
            if not protocol._same_field(native, snapshot.get(native), "0"):
                raise ValueError("native_credit_initial_tax_unverified")
            # A missing REST field is resolved by an actual native observation.
            before[rest] = snapshot[native]
        elif before.get(rest) is None or not protocol._same_field(native, snapshot.get(native), before[rest]):
            raise ValueError("native_preview_evidence_changed")
    for native, rest in (
        ("entity", "entity"),
        ("account", "account"),
        ("currency", "currency"),
        ("subsidiary", "subsidiary"),
    ):
        identifier = (before.get(rest) or {}).get("id")
        if identifier is None and native == "account" and proposal["record_type"] == "salesorder":
            if snapshot.get(native) is not None:
                raise ValueError("native_preview_identity_changed")
        elif identifier is None or snapshot.get(native) != str(identifier):
            raise ValueError("native_preview_identity_changed")
    return {
        **proposal,
        "native_profile": deepcopy(profile),
        "native_request": request,
        "native_preview": response,
        "status": "ready_for_exact_human_approval",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "financial_write_authorized": False,
    }


async def confirmation(db, tenant_id, actor_id, session_id, proposal, policy, correlation_id):
    from app.services.audit_service import log_event
    from app.services.chat.tools import netsuite_environment_of
    from app.services.chat.write_confirmation_service import WriteConfirmationPayload, mint_confirmation_token
    from app.services.policy_service import evaluate_tool_call

    params = signed_input(proposal)
    validate_binding(tenant_id, TOOL, params, proposal)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(proposal["observed_at"])).total_seconds()
    if not 0 <= age <= 300 or not evaluate_tool_call(policy, TOOL, params)["allowed"]:
        raise ValueError("native_proposal_policy_or_freshness_changed")
    card = WriteConfirmationPayload(
        mutation_type="update",
        record_type=proposal["record_type"],
        record_id=proposal["record_id"],
        proposed_fields=proposal["proposed_fields"],
        current_record=proposal["before"],
        tool_name=TOOL,
        tool_input=params,
        confirmation_token=mint_confirmation_token(TOOL, params, [], session_id),
        target_account=proposal["scope"]["netsuite_account_id"],
        target_environment=netsuite_environment_of(proposal["scope"]["netsuite_account_id"]),
        accounting_review=proposal,
    )
    await log_event(
        db,
        tenant_id,
        "transaction_ops",
        "accounting_correction.proposed",
        actor_id=actor_id,
        resource_type="transaction_case",
        resource_id=proposal["case_id"],
        correlation_id=correlation_id,
        status="pending",
        payload={
            "session_id": session_id,
            "proposal_digest": params["proposal_digest"],
            "record_id": proposal["record_id"],
            "scope": proposal["scope"],
            "before": proposal["before"],
            "proposed_fields": proposal["proposed_fields"],
            "expected_after": proposal["expected_after"],
            "approval_basis": proposal["approval_basis"],
            "approval_required": True,
            "financial_writes": 0,
        },
    )
    return card, f"Review the exact correction for {proposal['order_reference']}. No change has been sent."


async def _fresh(db, tenant_id, proposal):
    from app.services.transaction_ops import case_service
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.credit_reallocation import collect_support
    from app.services.transaction_ops.tax_correction import refresh_source

    case = await case_service.get_case(db, tenant_id, UUID(proposal["case_id"]))
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if (
        review.get("scope") != proposal["scope"]
        or review.get("config_id") != proposal["config_id"]
        or review.get("netsuite_connection_id") != proposal["connection_id"]
        or review.get("connection_active") is not True
    ):
        raise ValueError("native_current_scope_changed")
    profile = await current_profile(db, tenant_id, review)
    if profile != proposal["native_profile"]:
        raise ValueError("native_current_policy_changed")
    source = await refresh_source(db, tenant_id, review["scope"], case.order_reference, include_accounting_detail=True)
    if source != proposal["source"]:
        raise ValueError("native_current_source_changed")
    evidence = await collect_accounting_evidence(
        db, tenant_id, review, case.latest_report_json, field_map=profile["fields"]
    )
    support = await collect_support(db, tenant_id, source, review, evidence, field_map=profile["fields"])
    if not support:
        raise ValueError("native_current_subledger_incomplete")
    return source, review, evidence, support, profile


def _stable(value):
    """Only collector timestamps may differ; native revision dates stay protected."""
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k != "observed_at"}
    return value


async def validate_approved(db, tenant_id, tool_name, params, proposal):
    from app.services.transaction_ops.case_resolution_scope import validate

    await validate(db, tenant_id, proposal)
    validate_binding(tenant_id, tool_name, params, proposal)
    source, review, evidence, support, profile = await _fresh(db, tenant_id, proposal)
    if _stable(support) != _stable(proposal["support"]):
        raise ValueError("native_current_subledger_changed")
    from app.services.transaction_ops.credit_reallocation import build_intent
    from app.services.transaction_ops.source_line_alignment import build_intent as align

    if proposal["kind"] == "credit_tax_reallocation":
        rebuilt = build_intent(
            tenant_id, proposal["case_id"], source, review, evidence, support, field_map=profile["fields"]
        )
    else:
        predecessor = proposal["posting_predecessor"]
        await verify_predecessor(db, tenant_id, predecessor, current=(source, review, evidence, support, profile))
        rebuilt = align(source, evidence, predecessor["proposal"], field_map=profile["fields"])
    if not rebuilt or any(
        rebuilt.get(k) != proposal.get(k) for k in ("proposed_fields", "expected_after", "record_id")
    ):
        raise ValueError("native_current_treatment_changed")
    capabilities = await _read(db, tenant_id, proposal, "capabilities", {"subsidiaryId": profile["subsidiary_id"]})
    if not protocol.validate_capabilities(profile, capabilities)["apply_enabled"]:
        raise ValueError("native_amendment_installation_not_enabled")
    preview = await _read(db, tenant_id, proposal, "preview", {"request": proposal["native_request"]})
    protocol.validate_preview(profile, proposal["native_request"], preview)
    if preview["beforeSnapshot"] != proposal["native_preview"]["beforeSnapshot"]:
        raise ValueError("native_approved_record_changed")


def _verify_ledger(proposal, support):
    gl = support["credit_gl"]
    if gl.get("complete") is not True:
        raise ValueError("native_credit_ledger_incomplete")
    actual = {"debit": {}, "credit": {}}
    for row in gl["rows"]:
        if str(row["accountingbook"]) != proposal["accounting_book"]:
            raise ValueError("native_credit_book_changed")
        account = str(row["account"])
        for side in actual:
            amount = Decimal(str(row.get(side) or 0))
            if not amount.is_finite() or amount < 0:
                raise ValueError("native_credit_ledger_invalid")
            if amount:
                actual[side][account] = actual[side].get(account, Decimal(0)) + amount
    expected = {
        side: {account: Decimal(value) for account, value in values.items() if Decimal(value)}
        for side, values in proposal["expected_ledger"].items()
    }
    if actual != expected:
        raise ValueError("native_credit_ledger_mismatch")


async def verify_after(db, tenant_id, proposal, receipt=None, *, _current=None):
    from app.services.transaction_ops.resolution_plan import operation_identity

    try:
        if receipt and (
            receipt.get("record_id", proposal["record_id"]) != proposal["record_id"]
            or receipt.get("record_type", proposal["record_type"]) != proposal["record_type"]
        ):
            raise ValueError("native_receipt_identity_conflict")
        source, review, evidence, support, profile = (
            _current if _current is not None else await _fresh(db, tenant_id, proposal)
        )
        if (
            source != proposal["source"]
            or profile != proposal["native_profile"]
            or review["scope"] != proposal["scope"]
            or review["config_id"] != proposal["config_id"]
            or review["netsuite_connection_id"] != proposal["connection_id"]
        ):
            raise ValueError("native_verification_scope_changed")
        request = proposal["native_request"]
        scope = {key: request[key] for key in ("accountId", "recordType", "recordId", "subsidiaryId", "currencyId")}
        result = await _read(db, tenant_id, proposal, "snapshot", scope)
        if (
            result.get("success") is not True
            or result.get("profile") != protocol.profile_binding(profile)
            or result.get("record_type") != proposal["record_type"]
            or result.get("record_id") != proposal["record_id"]
            or type(result.get("financial_writes")) is not int
            or result["financial_writes"] != 0
            or result.get("execution_authorized") is not False
        ):
            raise ValueError("native_snapshot_scope_unverified")
        after = result["native_snapshot"]
        observed_record = (
            support["credit"] if proposal["kind"] == "credit_tax_reallocation" else evidence["sections"]["sales_order"]
        )
        _same_observation({**proposal, "before": observed_record}, after)
        protocol.verify_snapshot(
            proposal["native_preview"]["beforeSnapshot"],
            after,
            json.loads(request["amendmentJson"]),
            json.loads(request["expectedJson"]),
            work_key=operation_identity(proposal),
        )
        before = proposal["support"]
        for key in ("refund_audit", "refund_allocation"):
            if _stable(support.get(key)) != _stable(before.get(key)):
                raise ValueError("native_refund_audit_evidence_changed")
        for key in (
            "invoice",
            "refund",
            "refund_graph",
            "invoice_gl",
            "currency",
            "item",
            "tax_item",
            "period",
            "book",
            "ar_account",
            "offset_account",
            "tax_account",
        ):
            if _stable(support[key]) != _stable(before[key]):
                raise ValueError("native_related_posting_evidence_changed")
        if proposal["kind"] == "credit_tax_reallocation":
            # Applications are a separate sublist, outside the native item
            # snapshot. Preserve their exact complete REST observations too.
            if before["credit"].get("application_evidence") != support["credit"].get("application_evidence") or not (
                support["credit"].get("application_evidence") or {}
            ).get("complete"):
                raise ValueError("native_credit_applications_changed")
            _verify_ledger(proposal, support)
        else:
            for key in ("credit", "credit_gl"):
                if _stable(support[key]) != _stable(before[key]):
                    raise ValueError("native_related_credit_changed")
            await verify_predecessor(
                db, tenant_id, proposal["posting_predecessor"], current=(source, review, evidence, support, profile)
            )
        return {
            "status": "verified",
            "record_type": proposal["record_type"],
            "record_id": proposal["record_id"],
            "credit_memo_id": support["credit"]["id"],
            "invoice": support["invoice"],
            "sales_order": evidence["sections"]["sales_order"],
            "after": after,
            "source_revision": source.get("updated_at"),
            "ledger": support["credit_gl"],
            "related_records_unchanged": True,
            "retry_allowed": False,
            "financial_writes": 0,
            "scope": "native_amendment_and_related_postings",
            "full_reconciliation_required": True,
        }
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "needs_review", "reason": str(exc), "retry_allowed": False, "financial_writes": 0}


async def verify_predecessor(db, tenant_id, predecessor, *, current=None):
    from app.services.transaction_ops.accounting_recovery import _authorize_read, _message

    message = await _message(db, tenant_id, UUID(predecessor["confirmation_id"]))
    so = message.structured_output if message else {}
    if (
        so.get("status") != "approved"
        or (so.get("accounting_verification") or {}).get("status") != "verified"
        or so.get("accounting_review") != predecessor["proposal"]
        or predecessor["proposal"].get("kind") != "credit_tax_reallocation"
    ):
        raise ValueError("native_posting_predecessor_unverified")
    await _authorize_read(db, tenant_id, message, so["accounting_execution"])
    verified = await verify_after(db, tenant_id, predecessor["proposal"], _current=current)
    if verified["status"] != "verified":
        raise ValueError("native_posting_predecessor_changed")
    return verified


async def prepare_alignment(db, tenant_id, case_id, source, review, evidence, support, profile):
    from sqlalchemy import select

    from app.models.chat import ChatMessage
    from app.services.transaction_ops.source_line_alignment import build_intent

    so = ChatMessage.structured_output
    messages = list(
        await db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.tenant_id == tenant_id,
                so["status"].astext == "approved",
                so["accounting_review"]["case_id"].astext == str(case_id),
                so["accounting_review"]["kind"].astext == "credit_tax_reallocation",
                so["accounting_verification"]["status"].astext == "verified",
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(5)
        )
    )
    for message in messages:
        posting = message.structured_output["accounting_review"]
        if (
            posting.get("source") != source
            or posting.get("scope") != review["scope"]
            or posting.get("native_profile") != profile
        ):
            continue
        predecessor = {"confirmation_id": str(message.id), "proposal": posting}
        await verify_predecessor(db, tenant_id, predecessor, current=(source, review, evidence, support, profile))
        alignment = build_intent(source, evidence, posting, field_map=profile["fields"])
        if not alignment:
            return None
        proposal = {
            **{
                key: posting[key]
                for key in (
                    "tenant_id",
                    "case_id",
                    "order_reference",
                    "invoice_id",
                    "sales_order_id",
                    "connection_id",
                    "config_id",
                    "scope",
                    "source",
                    "accounting_book",
                    "ar_account",
                    "sales_adjustment_account",
                    "tax_account",
                    "tax_item",
                    "tax_agency",
                    "period",
                    "connector_id",
                )
            },
            **alignment,
            "before": evidence["sections"]["sales_order"],
            "support": support,
            "posting_predecessor": predecessor,
            "lock_record_type": "invoice",
            "mutation_type": "update",
        }
        return await preview_candidate(db, tenant_id, proposal, review, profile)
    return None
