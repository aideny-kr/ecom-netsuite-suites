"""Seeded-tenant publishable-report lifecycle e2e (Slice 1 — report regression backbone).

Codifies the compose -> view report lifecycle as a deterministic, CI-runnable test
against the real test DB. It drives the REAL resolver path that ``report.compose``
uses (``load_conversation_tool_messages`` + ``resolve_payload_from_messages`` from
``app/services/chat/tool_call_results.py``) and the REAL compose orchestration
(``app/services/report/report_service.compose_report``) + the REAL HTTP view
endpoint (``app/api/v1/reports.py``) — no reimplementation.

Invariants (authoritative list is inline below; design rationale lives in the plan
docs/superpowers/plans/2026-06-09-publishable-report-renderer.md §16.1 / §16.2 / §11):
  I1 FULL-ROWS regression guard (§16.1): the resolver returns the FULL, uncapped
     frozen payload from ``ChatMessage.tool_calls[].result_payload`` (NOT the
     50-row Redis result cache), so a 60-row source survives into the persisted
     ``reports.rendered_html`` — row 60's value is present, not capped at 50.
  I2 GET /api/v1/reports/{id}/view returns the rendered HTML (200, text/html) to
     the owning tenant.
  I3 Cross-tenant GET -> 404 (RLS-invisible row, no existence disclosure; §11).
  I4 Trust boundary (§4): the condensed string handed back to the LLM (built by the
     orchestrator's ``_intercept_tool_result`` report branch) carries NO figures
     from the data — a known cell value is absent.
  I5 An ``audit_events`` row with ``action='report.compose'`` exists for the report.
"""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import func, select, text

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.models.report import Report
from app.services.chat.orchestrator import _intercept_tool_result
from app.services.chat.tool_call_results import (
    load_conversation_tool_messages,
    resolve_payload_from_messages,
)
from app.services.report.report_service import compose_report
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

API = "/api/v1/reports"

# A 60-row table — deliberately > the 50-row Redis result cap (CachedResult.to_json
# truncates rows[:50]). Each row's value embeds its 1-based index so we can probe a
# specific row by a unique, greppable string in the rendered HTML.
_ROW_COUNT = 60


def _row_marker(i: int) -> str:
    """A unique, HTML-safe sentinel value for row ``i`` (1-based)."""
    return f"ROWVALUE-{i:03d}"


def _full_table_payload() -> dict:
    """The FULL, uncapped frozen payload exactly as ``extract_result_payload`` builds it
    for a columns/rows tool result (kind='table'). 60 rows -> proves no 50-row cap."""
    rows = [[f"P{i:03d}", _row_marker(i)] for i in range(1, _ROW_COUNT + 1)]
    return {
        "kind": "table",
        "columns": ["Period", "Revenue"],
        "rows": rows,
        "row_count": _ROW_COUNT,
        "truncated": False,
        "query": "SELECT period, revenue FROM sales",
        "limit": _ROW_COUNT,
    }


async def _seed_assistant_message_with_payload(db, tenant, user, payload: dict) -> uuid.UUID:
    """Seed a ChatSession + an assistant ChatMessage whose tool_calls carry the FULL
    frozen ``result_payload``. Returns the session id (== the conversation_id the
    compose tool resolves against). RLS context must be set by the caller."""
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="Q2 review")
    db.add(session)
    await db.flush()

    message = ChatMessage(
        tenant_id=tenant.id,
        session_id=session.id,
        role="assistant",
        content="Here is the revenue breakdown.",
        tool_calls=[
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT period, revenue FROM sales"},
                "result_summary": f"Returned {payload['row_count']} rows",
                "result_payload": payload,
            }
        ],
    )
    db.add(message)
    await db.flush()
    return session.id


# ---------------------------------------------------------------------------
# I1 + I2 + I4 + I5 — compose via the REAL resolver path, then assert
#   full rows survive into the persisted HTML, the view endpoint serves it,
#   the LLM-condensed string has no figures, and a compose audit row exists.
# ---------------------------------------------------------------------------


async def test_compose_resolves_full_rows_views_html_and_audits(db, client):
    tenant = await create_test_tenant(db, name="Report Corp A")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))

    payload = _full_table_payload()
    conversation_id = await _seed_assistant_message_with_payload(db, tenant, user, payload)

    # --- Drive the REAL resolver path that report.compose uses (NOT a stub) ---
    messages = await load_conversation_tool_messages(db, conversation_id)

    def resolver(rid: str) -> dict:
        return resolve_payload_from_messages(messages, rid)

    # The resolver returns the FULL uncapped payload (60 rows), not a 50-row cap.
    resolved = resolver("r1")
    assert len(resolved["rows"]) == _ROW_COUNT, "resolver must return all 60 rows, uncapped"

    sections = [
        {"type": "heading", "level": 1, "text": "Q2 Revenue Review"},
        {"type": "narrative", "markdown": "Top period revenue was {{result:r1.row_count}} periods."},
        {"type": "table", "result_id": "r1"},
    ]
    result = await compose_report(
        db,
        tenant_id=tenant.id,
        title="Q2 Revenue Review",
        sections=sections,
        resolver=resolver,
        created_by=user.id,
        source_run_id=conversation_id,
    )
    report_id = result["report_id"]

    # --- I1: persisted rendered_html carries row 60 (NOT capped at 50) ---
    report = (
        await db.execute(select(Report).where(Report.id == uuid.UUID(report_id)))
    ).scalar_one()
    assert _row_marker(_ROW_COUNT) in report.rendered_html, "row 60 must survive into the HTML (no 50-cap)"
    assert _row_marker(50) in report.rendered_html  # sanity: a mid-table row is also present
    assert _row_marker(1) in report.rendered_html
    # The persisted spec's table section must itself carry all 60 rows (frozen, uncapped).
    table_section = next(s for s in report.spec_json["sections"] if s["type"] == "table")
    assert len(table_section["rows"]) == _ROW_COUNT

    # --- I2: GET /view returns the HTML to the owner (200, text/html) ---
    resp = await client.get(f"{API}/{report_id}/view", headers=make_auth_headers(user))
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert _row_marker(_ROW_COUNT) in resp.text  # full rows served over HTTP too

    # --- I4: trust boundary — the LLM-condensed string carries NO figures ---
    # Serialize the compose result exactly as it flows through the tool seam, then
    # run the REAL orchestrator interception branch the report path uses.
    event_type, sse, condensed = _intercept_tool_result("report_compose", json.dumps(result))
    assert event_type == "report_ready"
    assert sse["report_id"] == report_id
    # A known cell value (row 60) must be absent from the LLM-facing condensed payload.
    assert _row_marker(_ROW_COUNT) not in condensed
    assert _row_marker(1) not in condensed
    # The row_count figure must not leak either. The condensed payload legitimately
    # carries the report_id UUID, whose hex can contain "60" by chance (observed flake)
    # — so assert against the condensed string with the id removed, and assert no
    # numeric data fields survived into the parsed payload.
    condensed_parsed = json.loads(condensed)
    condensed_sans_id = condensed.replace(condensed_parsed["report_id"], "")
    assert "60" not in condensed_sans_id  # row_count figure absent outside the opaque id
    assert "row_count" not in condensed_parsed
    assert "rows" not in condensed_parsed

    # --- I5: an audit_events row with action='report.compose' exists for the report ---
    audit_count = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "report.compose",
                AuditEvent.resource_id == report_id,
                AuditEvent.tenant_id == tenant.id,
            )
        )
    ).scalar_one()
    assert audit_count == 1


# ---------------------------------------------------------------------------
# I3 — cross-tenant GET /view -> 404 (RLS-invisible; no existence disclosure)
# ---------------------------------------------------------------------------


async def test_view_cross_tenant_is_404(db, client):
    """The endpoint's cross-tenant 404 is driven entirely by RLS hiding the row
    (``_get_owned`` -> ``scalar_one_or_none()`` None -> 404, spec §11).

    The local ``db`` fixture connects as the BYPASSRLS ``postgres`` owner, which sees
    every row REGARDLESS of FORCE RLS — so asserting the HTTP 404 directly through the
    shared session would pass vacuously / falsely. We therefore prove invisibility
    GENUINELY: write the report as tenant A, then SELECT it under tenant B's context
    through a fresh NOLOGIN non-bypass role so the SELECT is actually subject to the
    FORCE'd policy (the same idiom as ``test_reports_api.py`` + ``test_report_migration.py``).
    The owner-can-view leg (200) is asserted over real HTTP; the cross-tenant leg is the
    RLS-policy proof. On managed Supabase (``postgres`` lacks CREATEROLE) this skips
    cleanly — there the live smoke (Task 15) is the authoritative cross-tenant gate."""
    tenant_a = await create_test_tenant(db, name="Report Corp A2")
    user_a, _ = await create_test_user(db, tenant_a)
    tenant_b = await create_test_tenant(db, name="Report Corp B2")

    await set_tenant_context(db, str(tenant_a.id))
    payload = _full_table_payload()
    conversation_id = await _seed_assistant_message_with_payload(db, tenant_a, user_a, payload)
    messages = await load_conversation_tool_messages(db, conversation_id)

    result = await compose_report(
        db,
        tenant_id=tenant_a.id,
        title="Tenant A report",
        sections=[{"type": "table", "result_id": "r1"}],
        resolver=lambda rid: resolve_payload_from_messages(messages, rid),
        created_by=user_a.id,
        source_run_id=conversation_id,
    )
    report_id = result["report_id"]

    # Owner (tenant A) can view over real HTTP.
    resp_a = await client.get(f"{API}/{report_id}/view", headers=make_auth_headers(user_a))
    assert resp_a.status_code == 200, resp_a.text

    # Cross-tenant invisibility — proven through a non-bypass role so the FORCE'd
    # policy actually applies (the BYPASSRLS owner would otherwise see the row, which
    # is exactly why a raw HTTP-404 assertion here is not a real policy proof).
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    conn = await db.connection()
    role = f"_rls_probe_{uuid.uuid4().hex[:12]}"

    class _CapturedError(Exception):
        rows: list

    try:
        async with conn.begin_nested():
            await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
            await conn.execute(text(f'GRANT SELECT ON reports TO "{role}"'))
            await conn.execute(text(f'GRANT EXECUTE ON FUNCTION get_current_tenant_id() TO "{role}"'))
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            captured = (await conn.execute(text("SELECT id FROM reports"))).all()  # NO .where
            await conn.execute(text("RESET ROLE"))
            exc = _CapturedError()
            exc.rows = captured
            raise exc  # abort the savepoint -> discards the role + grants cleanly
    except _CapturedError as done:
        rows = done.rows
    except (sqlalchemy.exc.ProgrammingError, sqlalchemy.exc.DBAPIError):
        pytest.skip(
            "cannot create/enter a non-bypass role here (managed Supabase) — the "
            "migration catalog test + the live smoke are the authoritative policy gates"
        )
    assert rows == [], "FORCE RLS must hide tenant A's report from tenant B (-> endpoint 404)"
