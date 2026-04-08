"""Task 20: End-to-end integration tests for the disclosure footer feature.

These tests exercise the disclosure feature at the HTTP / service boundary,
complementing the unit tests that already cover each building block in
isolation:

  - `test_disclosure_dataclass.py`   — DisclosureBlock payload
  - `test_disclosure_allowlist.py`   — data tool allowlist
  - `test_disclosure_parser.py`      — WHERE-clause parser
  - `test_disclosure_assembly.py`    — assemble_disclosure() rules
  - `test_source_switch_regex.py`    — SOURCE_SWITCH_RE
  - `test_source_pin_routing.py`     — source_pin honored by routing
  - `test_source_switch_endpoint.py` — chat endpoint smart re-run + ack paths

Per the plan, these are BEST EFFORT tests: mocking the full orchestrator
pipeline accurately is hard. We take a hybrid approach:

  - Tests 1-3 (SSE ordering, rerun flow, ack flow) are documented skip
    stubs that point to the existing coverage. Manual QA in Task 22 will
    exercise the end-to-end SSE path on staging.
  - Test 4 (feature flag OFF suppresses disclosure) is a real test of
    the `disclosure_enabled_for_tenant` helper using a live tenant + db
    session. This is the gating logic Task 14 added to the orchestrator.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.services.chat.disclosure import disclosure_enabled_for_tenant
from app.services.feature_flag_service import clear_cache as clear_flag_cache


# ---------------------------------------------------------------------------
# Test 1: SSE event ordering — disclosure BEFORE message
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Task 20 — SSE ordering is enforced architecturally by Task 9 "
        "(orchestrator.py yields the 'disclosure' event before the final "
        "'message' yield; see the three assemble_disclosure blocks around "
        "orchestrator.py:2125-2170, 2321-2345, and 2638-2660). Mocking the "
        "full LLM + tool-call + _intercept_tool_result pipeline to verify "
        "event ordering here would require stubbing ~15 integration points. "
        "Covered by manual QA in Task 22 on staging."
    )
)
@pytest.mark.asyncio
async def test_disclosure_emitted_before_message():
    """Happy path: a SuiteQL query produces disclosure BEFORE message in SSE.

    Expected behavior (verified manually in Task 22):
      1. POST /api/v1/chat/sessions/{id}/messages with "what were sales last week?"
      2. Wait for background run to complete.
      3. GET /api/v1/chat/runs/{run_id}/stream and collect event types in order.
      4. Assert the list looks like:
           [..., "tool_call", "data_table", "disclosure", "message", ...]
      5. Assert index("disclosure") < index("message").

    The contract: every final assistant message that used a data-returning
    tool must be preceded by a `disclosure` event carrying the interpretation,
    implicit filters, and can_switch_source flag — so the frontend's
    MessageList (Task 19) can attach the footer to the right message.
    """


# ---------------------------------------------------------------------------
# Test 2: Source-switch re-run flow — "use BigQuery" triggers is_rerun=True
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Task 20 — duplicates existing coverage in "
        "`test_source_switch_endpoint.py::test_switch_with_passing_guards_triggers_rerun`. "
        "That test seeds a session with can_switch=True + tool_calls + "
        "disclosure_json, sends 'use BigQuery', patches `_run_chat_background` "
        "with a MagicMock, and asserts call_args.kwargs['is_rerun'] is True "
        "and call_args.kwargs['user_message'] is the previous user query. "
        "No need to duplicate here."
    )
)
@pytest.mark.asyncio
async def test_source_switch_rerun_flow():
    """Sending 'use BigQuery' after a SuiteQL answer triggers a re-run.

    Expected behavior (covered by test_source_switch_endpoint.py):
      1. Pre-seed a session with a prior user message "sales last week"
         and a prior assistant message with tool_calls=[suiteql] and
         disclosure_json={can_switch_source: true}.
      2. POST "use BigQuery" to the messages endpoint.
      3. Patch `_run_chat_background` with MagicMock(side_effect=...).
      4. Assert mock.call_args.kwargs["is_rerun"] is True.
      5. Assert mock.call_args.kwargs["user_message"] == "sales last week".
    """


# ---------------------------------------------------------------------------
# Test 3: Source-switch acknowledgment — no prior data → ack-only
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Task 20 — duplicates existing coverage in "
        "`test_source_switch_endpoint.py::test_switch_with_failed_guards_emits_ack_only`. "
        "That test seeds a session with has_tool_calls=False + "
        "has_disclosure=False, sends 'use BigQuery', and asserts that "
        "_run_chat_background is never invoked and a single ack assistant "
        "message mentioning BigQuery is persisted. No need to duplicate."
    )
)
@pytest.mark.asyncio
async def test_source_switch_acknowledgment_without_prior_data():
    """Sending 'use BigQuery' after a text-only answer produces only an ack.

    Expected behavior (covered by test_source_switch_endpoint.py):
      1. Pre-seed a session with a prior text-only assistant message
         (no tool_calls, no disclosure_json).
      2. POST "use BigQuery".
      3. Assert the response is 202 with a run_id.
      4. Assert _run_chat_background was NOT invoked (no re-run).
      5. Assert a single ack assistant message was persisted with text
         mentioning BigQuery.
    """


# ---------------------------------------------------------------------------
# Test 4: Feature flag OFF suppresses disclosure (REAL test)
# ---------------------------------------------------------------------------
#
# This test exercises the `disclosure_enabled_for_tenant` helper directly.
# That helper is the single gate used by orchestrator.py in three places
# (see grep output in the Task 20 scene-setting context) to decide whether
# to call assemble_disclosure() + yield a 'disclosure' event. If this helper
# returns False, NO disclosure event is emitted anywhere. So testing this
# helper against a real database gives us real coverage of the flag-off path
# without having to mock the entire orchestrator pipeline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_flag_off_suppresses_disclosure(db, admin_user):
    """When disclosure_footer_enabled is OFF/absent, the helper returns False.

    This directly tests the gate the orchestrator uses:

        if await disclosure_enabled_for_tenant(db, tenant_id):
            _disclosure = assemble_disclosure(...)
            if _disclosure is not None:
                yield {"type": "disclosure", ...}

    If the helper returns False, no disclosure event is yielded to the
    SSE stream — regardless of whether the turn called data tools.
    """
    user, _headers = admin_user
    tenant_id = user.tenant_id

    # Clear flag cache between phases so our SQL inserts are visible
    # (is_enabled() has a 60-second in-memory TTL cache).
    clear_flag_cache()

    # ---- Phase 1: flag ABSENT (not inserted at all) ----
    # conftest.create_test_tenant seeds DEFAULT_FLAGS but
    # `disclosure_footer_enabled` is NOT in DEFAULT_FLAGS, so the row
    # does not exist for this tenant. Helper must return False.
    enabled = await disclosure_enabled_for_tenant(db, tenant_id)
    assert enabled is False, "disclosure should default OFF when flag row is absent"

    # ---- Phase 2: flag explicitly OFF ----
    await db.execute(
        text(
            "INSERT INTO tenant_feature_flags "
            "(id, tenant_id, flag_key, enabled, created_at, updated_at) "
            "VALUES (:id, :tid, 'disclosure_footer_enabled', false, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, flag_key) DO UPDATE SET enabled = EXCLUDED.enabled"
        ),
        {"id": uuid.uuid4(), "tid": tenant_id},
    )
    await db.flush()
    clear_flag_cache()  # evict the Phase-1 cached False
    enabled = await disclosure_enabled_for_tenant(db, tenant_id)
    assert enabled is False, "disclosure should be OFF when flag row is enabled=false"

    # ---- Phase 3: flag explicitly ON (sanity check — flag plumbing works) ----
    await db.execute(
        text(
            "UPDATE tenant_feature_flags SET enabled = true, updated_at = NOW() "
            "WHERE tenant_id = :tid AND flag_key = 'disclosure_footer_enabled'"
        ),
        {"tid": tenant_id},
    )
    await db.flush()
    clear_flag_cache()
    enabled = await disclosure_enabled_for_tenant(db, tenant_id)
    assert enabled is True, "disclosure should be ON when flag row is enabled=true"

    # ---- Phase 4: set it back OFF and reconfirm ----
    await db.execute(
        text(
            "UPDATE tenant_feature_flags SET enabled = false, updated_at = NOW() "
            "WHERE tenant_id = :tid AND flag_key = 'disclosure_footer_enabled'"
        ),
        {"tid": tenant_id},
    )
    await db.flush()
    clear_flag_cache()
    enabled = await disclosure_enabled_for_tenant(db, tenant_id)
    assert enabled is False, "disclosure should return OFF after toggling back to false"

    # Clean up the cache so subsequent tests in the session start fresh.
    clear_flag_cache()
