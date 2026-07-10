"""Chat tools — ``recon.get_resolution_summary`` (read) + ``recon.approve_group``
(HITL confirmation card). Task 7 of the ResolutionAgent tail plan.

Covers:
(a) summary tool happy path + missing-context error + truncation honesty
(b) approve tool executes via ``approve_group_core`` and approves the seeded
    fee group (both-convention kwargs)
(c) approve tool surfaces an HTTPException(403) from the core as a structured
    ``{"success": False, "error": ...}`` when ``recon_resolution_ui`` is off
(d) token round-trip: ``build_recon_group_confirmation`` payload →
    ``validate_and_extract_confirmation`` returns ``(True, "recon.approve_group",
    tool_input)``; a tampered ``tool_input`` invalidates the token
(e) ``test_prompt_tool_sync.py`` stays green (recon tools remain dormant —
    not in ALLOWED_CHAT_TOOLS — so no registry additions there)
"""

from __future__ import annotations

from decimal import Decimal

from app.mcp.tools import recon_approve_group, recon_resolution_summary
from app.services.chat.write_confirmation_service import (
    build_recon_group_confirmation,
    validate_and_extract_confirmation,
)
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def _seed_fee_group(db, tenant):
    """One above-materiality-free fee_line row that plans to
    fees:book_fee_line:deposit."""
    from app.api.v1.reconciliation import plan_resolutions

    user, _ = await create_test_user(db, tenant)
    await enable_feature_flag(db, tenant.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant.id, status="completed")
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


# ---------------------------------------------------------------------------
# (a) recon.get_resolution_summary
# ---------------------------------------------------------------------------


async def test_resolution_summary_happy_path(db, tenant_a):
    user, run = await _seed_fee_group(db, tenant_a)

    result = await recon_resolution_summary.execute({"run_id": str(run.id)}, db=db, tenant_id=tenant_a.id)

    assert result["success"] is True
    assert result["run_id"] == str(run.id)
    assert result["proposals_count"] == 1
    assert result["group_count"] == len(result["groups"])
    assert result["truncated"] is False
    group = result["groups"][0]
    assert group["group_key"] == "fees:book_fee_line:deposit"
    assert group["root_cause"] == "fees"
    assert group["action"] == "book_fee_line"
    assert isinstance(group["total_amount"], str)
    assert Decimal(group["total_amount"]) == Decimal("9.00")


async def test_resolution_summary_missing_context_error():
    result = await recon_resolution_summary.execute({"run_id": "x"})
    assert result == {"success": False, "error": "Missing database session or tenant context"}


async def test_resolution_summary_missing_run_id(db, tenant_a):
    result = await recon_resolution_summary.execute({}, db=db, tenant_id=tenant_a.id)
    assert result["success"] is False
    assert "run_id" in result["error"]


async def test_resolution_summary_truncation_honesty(db, tenant_a, monkeypatch):
    """Cap the group list to 1 via monkeypatch — group_count still reports
    the TRUE total and truncated flips True even though there's only one
    real group here (proves the comparison, not the seeded scale)."""
    monkeypatch.setattr(recon_resolution_summary, "_MAX_GROUPS", 0)
    user, run = await _seed_fee_group(db, tenant_a)

    result = await recon_resolution_summary.execute({"run_id": str(run.id)}, db=db, tenant_id=tenant_a.id)
    assert result["success"] is True
    assert result["groups"] == []
    assert result["group_count"] == 1
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# (b) recon.approve_group — direct approve via approve_group_core
# ---------------------------------------------------------------------------


async def test_approve_group_approves_seeded_fee_group(db, tenant_a):
    user, run = await _seed_fee_group(db, tenant_a)

    result = await recon_approve_group.execute(
        {"run_id": str(run.id), "group_key": "fees:book_fee_line:deposit"},
        db=db,
        tenant_id=tenant_a.id,
        actor_id=user.id,
    )

    assert result["success"] is True
    assert result["approved_count"] == 1
    assert result["skipped_count"] == 0
    assert result["correlation_id"]


async def test_approve_group_both_convention_context_kwargs(db, tenant_a):
    """Same call, but through the context= convention (the real chat dispatch
    shape via governed_execute)."""
    user, run = await _seed_fee_group(db, tenant_a)

    result = await recon_approve_group.execute(
        {"run_id": str(run.id), "group_key": "fees:book_fee_line:deposit"},
        context={"db": db, "tenant_id": tenant_a.id, "actor_id": user.id},
    )

    assert result["success"] is True
    assert result["approved_count"] == 1


async def test_approve_group_missing_context_error():
    result = await recon_approve_group.execute({"run_id": "x", "group_key": "a:b:c"})
    assert result == {"success": False, "error": "Missing database session or tenant context"}


# ---------------------------------------------------------------------------
# (c) HTTPException(403) from the core (recon_resolution_ui off) shapes as a
# structured tool error, never an uncaught exception.
# ---------------------------------------------------------------------------


async def test_approve_group_surfaces_ui_flag_off_as_structured_error(db, tenant_a):
    from app.api.v1.reconciliation import plan_resolutions

    user, _ = await create_test_user(db, tenant_a)
    # Enable to plan (plan_resolutions also gates on recon_resolution_ui),
    # then turn it OFF before approving — this test is specifically about
    # the approve-time gate, not the plan-time one.
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui", enabled=False)

    result = await recon_approve_group.execute(
        {"run_id": str(run.id), "group_key": "fees:book_fee_line:deposit"},
        db=db,
        tenant_id=tenant_a.id,
        actor_id=user.id,
    )

    assert result["success"] is False
    assert "recon_resolution_ui" in result["error"]


# ---------------------------------------------------------------------------
# (d) confirmation-token round-trip
# ---------------------------------------------------------------------------


def test_build_recon_group_confirmation_round_trips():
    tool_input = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "group_key": "fees:book_fee_line:deposit",
        "currency": "USD",
        "notes": "batch approve",
    }
    payload = build_recon_group_confirmation(tool_input=tool_input, session_id="session-1")

    assert payload.type == "write_confirmation"
    assert payload.mutation_type == "update"
    assert payload.record_type == "reconciliation group"
    assert payload.tool_name == "recon.approve_group"
    assert payload.proposed_fields["group"] == "fees:book_fee_line:deposit"
    assert payload.proposed_fields["currency"] == "USD"
    assert "items" not in payload.proposed_fields

    structured_output = payload.model_dump()
    structured_output["status"] = "pending"
    structured_output["type"] = "write_confirmation"

    is_valid, tool_name, extracted_input = validate_and_extract_confirmation(structured_output, "session-1")
    assert is_valid is True
    assert tool_name == "recon.approve_group"
    assert extracted_input == tool_input


def test_build_recon_group_confirmation_tampered_input_invalidates():
    tool_input = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "group_key": "fees:book_fee_line:deposit",
    }
    payload = build_recon_group_confirmation(tool_input=tool_input, session_id="session-1")

    structured_output = payload.model_dump()
    structured_output["status"] = "pending"
    structured_output["type"] = "write_confirmation"
    # Tamper the group_key post-signing — a smuggled group swap.
    structured_output["tool_input"] = {**tool_input, "group_key": "fees:book_fee_line:je"}

    is_valid, _tool_name, _extracted = validate_and_extract_confirmation(structured_output, "session-1")
    assert is_valid is False


# ---------------------------------------------------------------------------
# (e) test_prompt_tool_sync.py stays green — recon tools remain dormant
# (not in ALLOWED_CHAT_TOOLS), so no registry additions are needed there.
# ---------------------------------------------------------------------------


def test_recon_tools_not_in_default_chat_schema():
    """recon.* tools stay off the default chat tool schema (dormant family,
    same as recon.get_exceptions/recon.approve_match pre-existing) — chat
    reaches them only via the confirmation-card path wired in base_agent.py,
    not via Claude calling them directly through build_local_tool_definitions."""
    from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

    assert "recon.get_resolution_summary" not in ALLOWED_CHAT_TOOLS
    assert "recon.approve_group" not in ALLOWED_CHAT_TOOLS
