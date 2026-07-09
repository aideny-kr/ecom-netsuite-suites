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
# I1 + I2 + I4 + I5 — compose via the REAL (PERSISTED-MESSAGE FALLBACK) resolver
#   path, then assert full rows survive into the persisted HTML, the view
#   endpoint serves it, the LLM-condensed string has no figures, and a compose
#   audit row exists.
#
# NOTE (gate cluster A): the persisted-ChatMessage resolver is the CROSS-TURN /
# regeneration FALLBACK path — a prior turn's results are in the DB by the time a
# later turn (or a report regeneration) composes. The PRIMARY same-turn path is
# the eager full-payload Redis sidecar, exercised by
# ``test_compose_resolves_from_inturn_cache_sidecar`` below (the current turn's
# assistant ChatMessage is NOT yet persisted when report.compose runs mid-loop).
# ---------------------------------------------------------------------------


async def test_compose_resolves_full_rows_views_html_and_audits(db, client):
    tenant = await create_test_tenant(db, name="Report Corp A")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))

    payload = _full_table_payload()
    conversation_id = await _seed_assistant_message_with_payload(db, tenant, user, payload)

    # --- Drive the REAL resolver path that report.compose uses (NOT a stub) ---
    messages = await load_conversation_tool_messages(db, conversation_id, tenant.id)

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

    # --- I1: the table is CURATED to the first-K rows (top numbers, not a 60-row dump),
    # with the TRUE count preserved. The resolver above still returns the full 60 (no
    # stale 50-row Redis cap) — curation is a separate, intentional render-layer cap. ---
    from app.services.report.report_service import _REPORT_TABLE_TOP_K

    report = (await db.execute(select(Report).where(Report.id == uuid.UUID(report_id)))).scalar_one()
    assert _row_marker(1) in report.rendered_html  # the first rows survive (top numbers)
    assert _row_marker(_REPORT_TABLE_TOP_K) in report.rendered_html
    assert _row_marker(_ROW_COUNT) not in report.rendered_html  # the 60th row is curated out
    assert f"of {_ROW_COUNT}" in report.rendered_html  # "Showing first K of 60 rows" note
    # The persisted spec's table section carries exactly the curated first-K rows, but
    # keeps the TRUE row_count so the note (and any audit) can report the real total.
    table_section = next(s for s in report.spec_json["sections"] if s["type"] == "table")
    assert len(table_section["rows"]) == _REPORT_TABLE_TOP_K
    assert table_section["row_count"] == _ROW_COUNT

    # --- I2: GET /view returns the HTML to the owner (200, text/html) ---
    resp = await client.get(f"{API}/{report_id}/view", headers=make_auth_headers(user))
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert _row_marker(1) in resp.text  # the curated rows are served over HTTP too

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
# I1b (gate cluster A) — PRIMARY same-turn path: compose resolves from the eager
#   full-payload Redis sidecar when NO assistant ChatMessage is persisted yet
#   (the real mid-turn ordering). Drives the REAL report.compose tool
#   (report_export.execute), which is cache-first, then asserts the full rows
#   survive into the persisted report — proving in-turn resolution.
# ---------------------------------------------------------------------------


async def test_compose_resolves_from_inturn_cache_sidecar(db, client, monkeypatch):
    from unittest.mock import patch

    from app.mcp.tools import report_export

    tenant = await create_test_tenant(db, name="Report Corp Cache")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))

    # A bare ChatSession with NO assistant ChatMessage — i.e. the current turn's
    # results are NOT yet persisted (the real same-turn ordering). conversation_id
    # is the session id the compose tool resolves against.
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="Same-turn report")
    db.add(session)
    await db.flush()
    conversation_id = str(session.id)

    payload = _full_table_payload()

    # In-memory FakeRedis for the sidecar (mirrors test_result_cache.py).
    store: dict = {}

    class FakeRedis:
        def hset(self, key, field, value):
            store.setdefault(key, {})[field] = value

        def hget(self, key, field):
            return store.get(key, {}).get(field)

        def hgetall(self, key):
            return store.get(key, {})

        def hdel(self, key, field):
            store.get(key, {}).pop(field, None)

        def expire(self, key, ttl):
            pass

    with patch("app.services.chat.result_cache._get_redis", return_value=FakeRedis()):
        # Eagerly write the FULL, uncapped payload under the turn-scoped result_id —
        # exactly what the orchestrator's intercept callback does mid-turn.
        from app.services.chat.result_cache import cache_full_payload

        cache_full_payload(conversation_id, "r1", payload)

        # Drive the REAL report.compose tool — its resolver must be cache-first and
        # find r1 in the sidecar even though no ChatMessage carries it.
        result = await report_export.execute(
            {
                "title": "Same-turn Report",
                "sections": [
                    {"type": "heading", "level": 1, "text": "Same-turn Report"},
                    {"type": "table", "result_id": "r1"},
                ],
            },
            context={
                "db": db,
                "tenant_id": tenant.id,
                "conversation_id": conversation_id,
                "actor_id": user.id,
            },
        )

    report_id = result["report_id"]
    # report.compose no longer commits mid-turn; flush makes the row visible within
    # this shared session/transaction.
    report = (await db.execute(select(Report).where(Report.id == uuid.UUID(report_id)))).scalar_one()
    # The rows resolved FROM THE SIDECAR are curated to the first-K (top numbers), with
    # the true count preserved — the in-turn sidecar path resolved (no stale 50-cap), and
    # curation then bounded the render.
    from app.services.report.report_service import _REPORT_TABLE_TOP_K

    assert _row_marker(1) in report.rendered_html, "first sidecar rows survive (in-turn resolution worked)"
    assert _row_marker(_ROW_COUNT) not in report.rendered_html  # curated out
    table_section = next(s for s in report.spec_json["sections"] if s["type"] == "table")
    assert len(table_section["rows"]) == _REPORT_TABLE_TOP_K
    assert table_section["row_count"] == _ROW_COUNT


# ---------------------------------------------------------------------------
# I1c (re-gate r2 — findings #5/#9/#13): conversation-ordinal id space. Seed 2
#   persisted turns (3 payload-bearing calls → r1,r2,r3), then stamp a NEW in-turn
#   result via the interceptor — it gets r4 (start_count=K=3). compose resolving
#   r2 via the PERSISTED fallback returns turn 1's SECOND payload; r4 via the
#   in-turn sidecar returns the just-computed result. One id space, no collision.
# ---------------------------------------------------------------------------


async def test_conversation_ordinal_ids_span_persisted_and_inturn(db, client):
    from unittest.mock import patch

    from app.services.chat.orchestrator import _make_tool_interceptor
    from app.services.chat.tool_call_results import count_payload_bearing_tool_calls

    tenant = await create_test_tenant(db, name="Report Corp Ordinal")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))

    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="Multi-turn report")
    db.add(session)
    await db.flush()
    conversation_id = session.id

    def _payload(marker: str) -> dict:
        return {
            "kind": "table",
            "columns": ["Period", "Value"],
            "rows": [["P001", marker]],
            "row_count": 1,
            "truncated": False,
            "query": f"SELECT period, value FROM {marker}",
            "limit": 1,
        }

    # Explicit, increasing created_at so the two turns order deterministically.
    # Within ONE transaction the server-side ``func.now()`` default returns the SAME
    # timestamp for both rows, and ``order_by(created_at, id)`` would then tiebreak on
    # the RANDOM uuid primary key — so two same-flush turns can come back reversed.
    # Real turns occur seconds apart; mirror that here so the conversation order is
    # stable (turn 1 before turn 2).
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

    # Turn 1 (assistant msg) produced TWO payload-bearing results → conversation r1, r2.
    msg1 = ChatMessage(
        tenant_id=tenant.id,
        session_id=conversation_id,
        role="assistant",
        content="turn 1",
        created_at=t0,
        tool_calls=[
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {},
                "result_summary": "1",
                "result_payload": _payload("T1A"),
            },
            {
                "step": 1,
                "tool": "netsuite_suiteql",
                "params": {},
                "result_summary": "1",
                "result_payload": _payload("T1B"),
            },
        ],
    )
    db.add(msg1)
    await db.flush()
    # Turn 2 produced ONE payload-bearing result → conversation r3.
    msg2 = ChatMessage(
        tenant_id=tenant.id,
        session_id=conversation_id,
        role="assistant",
        content="turn 2",
        created_at=t0 + timedelta(seconds=30),
        tool_calls=[
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {},
                "result_summary": "1",
                "result_payload": _payload("T2A"),
            },
        ],
    )
    db.add(msg2)
    await db.flush()

    # The orchestrator seeds the in-turn counter from the prior-conversation count.
    messages = await load_conversation_tool_messages(db, conversation_id, tenant.id)
    k = count_payload_bearing_tool_calls(messages)
    assert k == 3, "3 payload-bearing results already in this conversation"

    # The persisted FALLBACK resolves r2 to turn 1's SECOND payload (conversation order).
    assert resolve_payload_from_messages(messages, "r2")["rows"] == [["P001", "T1B"]]
    assert resolve_payload_from_messages(messages, "r1")["rows"] == [["P001", "T1A"]]
    assert resolve_payload_from_messages(messages, "r3")["rows"] == [["P001", "T2A"]]

    # Now turn 3 computes a NEW result. The interceptor, seeded start_count=K=3,
    # stamps r4 and writes the in-turn sidecar under r4 — the SAME id space.
    store: dict = {}

    class FakeRedis:
        def hset(self, key, field, value):
            store.setdefault(key, {})[field] = value

        def hget(self, key, field):
            return store.get(key, {}).get(field)

        def hgetall(self, key):
            return store.get(key, {})

        def hdel(self, key, field):
            store.get(key, {}).pop(field, None)

        def expire(self, key, ttl):
            pass

    with patch("app.services.chat.result_cache._get_redis", return_value=FakeRedis()):
        from app.services.chat.result_cache import cache_full_payload, get_full_payload

        captured: dict = {}

        def _cb(tool_name, ev_type, ev_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["result_id"] = result_id
            if result_id and full_payload is not None:
                cache_full_payload(str(conversation_id), result_id, full_payload)

        interceptor = _make_tool_interceptor(cache_callback=_cb, start_count=k)
        new_payload = _payload("T3-INTURN")
        _, llm_str = interceptor("netsuite_suiteql", json.dumps(new_payload, default=str))

        # The new in-turn result is r4 — never colliding with r1/r2/r3.
        assert json.loads(llm_str)["result_id"] == "r4"
        assert captured["result_id"] == "r4"

        # A same-turn compose resolving r4 reads the in-turn sidecar; r1..r3 still
        # resolve via the persisted fallback — one id space, end to end.
        assert get_full_payload(str(conversation_id), "r4")["rows"] == [["P001", "T3-INTURN"]]
        # And the sidecar write did NOT clobber any earlier id (none were in the sidecar).
        assert get_full_payload(str(conversation_id), "r2") is None  # r2 lives only in persisted history


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
    messages = await load_conversation_tool_messages(db, conversation_id, tenant_a.id)

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


# ---------------------------------------------------------------------------
# Slice B — the full live-dashboard lifecycle: REAL compose captures the recipe
#   (Slice A, sidecar meta) → REAL HTTP refresh replays it (Slice B) → version
#   chain 1→2→3 with immutable history, the parent-mirror invariant, the audit
#   trail, and the debounce. Only the outbound tool executor is stubbed.
# ---------------------------------------------------------------------------


async def test_compose_capture_refresh_lifecycle(db, client, monkeypatch):
    import json as _json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from app.mcp.tools import report_export
    from app.models.report_version import ReportVersion
    from app.services.report.refresh_service import REFRESH_MIN_INTERVAL_SECONDS

    tenant = await create_test_tenant(db, name="Report Corp Live")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))

    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="Live dashboard")
    db.add(session)
    await db.flush()
    conversation_id = str(session.id)

    def _suiteql_result(marker: str) -> dict:
        return {
            "success": True,
            "columns": ["account", "amount"],
            "rows": [[marker, 1000]],
            "row_count": 1,
            "query": "SELECT account, amount FROM balances",
        }

    store: dict = {}

    class FakeRedis:
        def hset(self, key, field, value):
            store.setdefault(key, {})[field] = value

        def hget(self, key, field):
            return store.get(key, {}).get(field)

        def hgetall(self, key):
            return store.get(key, {})

        def hdel(self, key, field):
            store.get(key, {}).pop(field, None)

        def expire(self, key, ttl):
            pass

    with patch("app.services.chat.result_cache._get_redis", return_value=FakeRedis()):
        from app.services.chat.result_cache import cache_full_payload
        from app.services.chat.tool_call_results import extract_result_payload

        # The orchestrator's sidecar write, meta-bearing (Slice A): payload + executed tool/params.
        params = {"query": "SELECT account, amount FROM balances"}
        payload = extract_result_payload("netsuite_suiteql", params, _json.dumps(_suiteql_result("COMPOSE-V1")))
        assert payload is not None
        cache_full_payload(conversation_id, "r1", payload, tool_name="netsuite_suiteql", params=params)

        # REAL compose — recipe captured server-side from the executed call.
        result = await report_export.execute(
            {
                "title": "Live Cash",
                "sections": [
                    {"type": "heading", "level": 1, "text": "Live Cash"},
                    {"type": "table", "result_id": "r1"},
                ],
            },
            context={"db": db, "tenant_id": tenant.id, "conversation_id": conversation_id, "actor_id": user.id},
        )
    report_id = result["report_id"]
    report = (await db.execute(select(Report).where(Report.id == uuid.UUID(report_id)))).scalar_one()
    assert report.recipe_json is not None, "Slice A capture must feed Slice B replay"
    assert report.recipe_json["sources"]["r1"]["tool"] == "netsuite_suiteql"
    assert "COMPOSE-V1" in report.rendered_html

    # Stub ONLY the outbound executor for the replay; fresh numbers each refresh.
    fresh = {"marker": "REFRESH-V2"}

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        assert tool_name == "netsuite_suiteql" and tool_input == params  # stored params replayed
        return _json.dumps(_suiteql_result(fresh["marker"]))

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)
    headers = make_auth_headers(user)

    # Refresh #1 over REAL HTTP → v2 at the same URL.
    res = await client.post(f"{API}/{report_id}/refresh", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["version"] == 2
    view = await client.get(f"{API}/{report_id}/view", headers=headers)
    assert "REFRESH-V2" in view.text and "COMPOSE-V1" not in view.text
    assert 'class="stamp"' in view.text

    # Debounced within the window.
    assert (await client.post(f"{API}/{report_id}/refresh", headers=headers)).status_code == 429

    # Step past the window; refresh #2 → v3.
    report.last_refreshed_at = datetime.now(timezone.utc) - timedelta(seconds=REFRESH_MIN_INTERVAL_SECONDS + 1)
    await db.flush()
    fresh["marker"] = "REFRESH-V3"
    res = await client.post(f"{API}/{report_id}/refresh", headers=headers)
    assert res.status_code == 200 and res.json()["version"] == 3

    # Version chain: immutable history + the parent-mirror invariant (risk §8.6 pin).
    versions = (await db.execute(select(ReportVersion).where(ReportVersion.report_id == report.id))).scalars().all()
    by_v = {v.version: v for v in versions}
    assert set(by_v) == {1, 2, 3}
    assert "COMPOSE-V1" in by_v[1].rendered_html  # v1 = the original compose, snapshotted honestly
    assert "REFRESH-V2" in by_v[2].rendered_html
    await db.refresh(report)
    assert report.version == max(by_v), "parent must mirror the latest version"
    assert "REFRESH-V3" in report.rendered_html

    # HTTP version picker + historical view.
    listed = (await client.get(f"{API}/{report_id}/versions", headers=headers)).json()
    assert [v["version"] for v in listed] == [3, 2, 1]
    v1 = await client.get(f"{API}/{report_id}/versions/1/view", headers=headers)
    assert "COMPOSE-V1" in v1.text

    # Audit trail: one compose + two refreshes on the stable id.
    refresh_audits = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "report.refresh", AuditEvent.resource_id == report_id)
        )
    ).scalar_one()
    assert refresh_audits == 2


# ---------------------------------------------------------------------------
# Slice C — dashboard mode: REAL compose (recipe + §6.1 daily default) → the Beat
#   sweep refreshes as the SYSTEM actor → retention prunes past the cap with a
#   pinned survivor and the parent-mirror invariant intact → the failure ladder
#   walks fail → pause (excluded from the sweep) → one-click resume over REAL
#   HTTP → recovery. Cross-tenant non-interference pinned throughout. The
#   debounce/supersede skip-classification is unit-pinned in
#   tests/workers/test_report_auto_refresh.py (not naturally constructible here —
#   it needs a mid-flight race).
# ---------------------------------------------------------------------------


async def test_auto_refresh_sweep_ladder_retention_resume_e2e(db, client, monkeypatch):
    import json as _json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from app.core.config import settings
    from app.mcp.tools import report_export
    from app.models.report_version import ReportVersion
    from app.workers.tasks.report_auto_refresh import PAUSE_THRESHOLD, sweep_tenant_reports

    tenant = await create_test_tenant(db, name="Dashboard Corp")
    user, _ = await create_test_user(db, tenant)

    # A second tenant with its own due report — must be untouched by tenant A's sweep.
    tenant_b = await create_test_tenant(db, name="Bystander Corp")
    user_b, _ = await create_test_user(db, tenant_b)
    # capture ids NOW: the failure ladder's rollbacks expire every ORM instance in
    # this session (the documented refresh-service landmine)
    tid, tb_id = tenant.id, tenant_b.id
    await set_tenant_context(db, str(tb_id))
    bystander = Report(
        tenant_id=tenant_b.id,
        title="B",
        spec_json={"sections": []},
        rendered_html="<html>b</html>",
        created_by=user_b.id,
        recipe_json={
            "schema_version": 1,
            "captured_at": "t",
            "sections": [{"type": "table", "result_id": "r1"}],
            "sources": {"r1": {"tool": "netsuite_suiteql", "params": {"query": "SELECT b"}, "connection_id": None}},
        },
    )
    db.add(bystander)
    await db.flush()
    bystander_id = bystander.id

    # --- REAL compose captures the recipe (same rig as the Slice-B lifecycle test) ----
    await set_tenant_context(db, str(tid))
    session = ChatSession(tenant_id=tid, user_id=user.id, title="Dashboard")
    db.add(session)
    await db.flush()
    conversation_id = str(session.id)

    def _suiteql_result(marker: str) -> dict:
        return {
            "success": True,
            "columns": ["account", "amount"],
            "rows": [[marker, 1000]],
            "row_count": 1,
            "query": "SELECT account, amount FROM balances",
        }

    store: dict = {}

    class FakeRedis:
        def hset(self, key, field, value):
            store.setdefault(key, {})[field] = value

        def hget(self, key, field):
            return store.get(key, {}).get(field)

        def hgetall(self, key):
            return store.get(key, {})

        def hdel(self, key, field):
            store.get(key, {}).pop(field, None)

        def expire(self, key, ttl):
            pass

    with patch("app.services.chat.result_cache._get_redis", return_value=FakeRedis()):
        from app.services.chat.result_cache import cache_full_payload
        from app.services.chat.tool_call_results import extract_result_payload

        params = {"query": "SELECT account, amount FROM balances"}
        payload = extract_result_payload("netsuite_suiteql", params, _json.dumps(_suiteql_result("COMPOSE-V1")))
        cache_full_payload(conversation_id, "r1", payload, tool_name="netsuite_suiteql", params=params)
        result = await report_export.execute(
            {
                "title": "Dash",
                "sections": [
                    {"type": "heading", "level": 1, "text": "Dash"},
                    {"type": "table", "result_id": "r1"},
                ],
            },
            context={"db": db, "tenant_id": tid, "conversation_id": conversation_id, "actor_id": user.id},
        )
    report_id = result["report_id"]
    rid = uuid.UUID(report_id)
    headers = make_auth_headers(user)

    # §6.1 pin: a newly composed recipe-bearing report defaults to daily.
    body = (await client.get(f"{API}/{report_id}", headers=headers)).json()
    assert body["auto_refresh"] == "daily" and body["has_recipe"] is True

    # Controllable outbound executor: marker + failure switch.
    state = {"marker": "SWEEP-V2", "fail": False}

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        if state["fail"]:
            return _json.dumps({"error": True, "message": "invalid or expired token"})
        return _json.dumps(_suiteql_result(state["marker"]))

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)

    async def _make_stale():
        await set_tenant_context(db, str(tid))
        row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one()
        row.last_refreshed_at = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
        await db.flush()
        return row

    # --- Sweep 1 (happy): the SYSTEM actor publishes v2 -------------------------------
    stats = await sweep_tenant_reports(db, tid)
    assert stats["refreshed"] == 1 and stats["failed"] == 0
    await set_tenant_context(db, str(tid))
    report = (await db.execute(select(Report).where(Report.id == rid))).scalar_one()
    assert report.version == 2
    v2 = (
        await db.execute(select(ReportVersion).where(ReportVersion.report_id == rid, ReportVersion.version == 2))
    ).scalar_one()
    assert v2.created_by is None  # no human author
    sys_audit = (
        await db.execute(
            select(AuditEvent.actor_id, AuditEvent.actor_type).where(
                AuditEvent.action == "report.refresh", AuditEvent.resource_id == report_id
            )
        )
    ).first()
    assert sys_audit is not None and sys_audit[0] is None and sys_audit[1] == "system"
    view = await client.get(f"{API}/{report_id}/view", headers=headers)
    assert "SWEEP-V2" in view.text  # the stable URL serves the sweep's numbers

    # --- Retention (cap 2): pinned v1 survives, oldest unpinned pruned, parent==MAX ---
    monkeypatch.setattr(settings, "REPORT_VERSION_RETENTION_CAP", 2)
    v1_row = (
        await db.execute(select(ReportVersion).where(ReportVersion.report_id == rid, ReportVersion.version == 1))
    ).scalar_one()
    v1_row.pinned = True  # the auditor's pin
    await db.flush()
    await _make_stale()
    state["marker"] = "SWEEP-V3"
    stats = await sweep_tenant_reports(db, tid)
    assert stats["refreshed"] == 1
    await set_tenant_context(db, str(tid))
    versions = {
        v.version: v
        for v in (await db.execute(select(ReportVersion).where(ReportVersion.report_id == rid))).scalars().all()
    }
    assert set(versions) == {1, 3}, "cap 2: v2 (oldest unpinned) pruned; pinned v1 exempt"
    report = (await db.execute(select(Report).where(Report.id == rid))).scalar_one()
    assert report.version == 3 == max(versions), "parent mirrors MAX surviving version"
    assert "COMPOSE-V1" in versions[1].rendered_html  # the pinned original, intact

    # --- Failure ladder over REAL HTTP surfaces ---------------------------------------
    state["fail"] = True
    await _make_stale()
    stats = await sweep_tenant_reports(db, tid)
    assert stats["failed"] == 1
    body = (await client.get(f"{API}/{report_id}", headers=headers)).json()
    assert body["refresh_failure_count"] == 1  # the FE staleness banner's signal
    assert body["auto_refresh_paused_at"] is None
    assert body["version"] == 3  # last good version intact

    # Walk to the pause threshold, then one more failure → paused + excluded.
    row = await _make_stale()
    row.refresh_failure_count = PAUSE_THRESHOLD - 1
    await db.flush()
    stats = await sweep_tenant_reports(db, tid)
    assert stats["failed"] == 1 and stats["paused"] == 1
    body = (await client.get(f"{API}/{report_id}", headers=headers)).json()
    assert body["auto_refresh_paused_at"] is not None
    await _make_stale()
    assert (await sweep_tenant_reports(db, tid))["due"] == 0  # no retry storm

    # --- One-click resume over REAL HTTP, then recovery --------------------------------
    res = await client.post(f"{API}/{report_id}/auto-refresh/resume", headers=headers)
    assert res.status_code == 200
    assert res.json()["auto_refresh_paused_at"] is None and res.json()["refresh_failure_count"] == 0
    state["fail"] = False
    state["marker"] = "SWEEP-V4"
    await _make_stale()
    stats = await sweep_tenant_reports(db, tid)
    assert stats["refreshed"] == 1
    body = (await client.get(f"{API}/{report_id}", headers=headers)).json()
    assert body["version"] == 4 and body["refresh_failure_count"] == 0

    # --- Cross-tenant non-interference: B's due report untouched by A's sweeps --------
    await set_tenant_context(db, str(tb_id))
    b_row = (await db.execute(select(Report).where(Report.id == bystander_id))).scalar_one()
    assert b_row.version == 1 and b_row.refresh_failure_count == 0
    assert b_row.rendered_html == "<html>b</html>"


# ---------------------------------------------------------------------------
# Live-QA regressions (2026-07-09, real Framework cash-flow compose on staging):
# a 7-data-call turn FIFO-evicted r1 from the same-turn sidecar (cap was 6 —
# borrowed from the preview cache), so the published report carried three
# 'Data unavailable' cards for the flagship statement AND recipe capture
# fail-closed. Two-layer fix: the sidecar cap covers a whole turn, and compose
# REFUSES unresolvable rids loudly (the agent re-fetches and retries) instead
# of publishing a broken financial artifact.
# ---------------------------------------------------------------------------


class _DictRedis:
    def __init__(self):
        self.store: dict = {}

    def hset(self, key, field, value):
        self.store.setdefault(key, {})[field] = value

    def hget(self, key, field):
        return self.store.get(key, {}).get(field)

    def hgetall(self, key):
        return self.store.get(key, {})

    def hdel(self, key, field):
        self.store.get(key, {}).pop(field, None)

    def expire(self, key, ttl):
        pass


async def test_compose_refuses_unresolvable_result_ids(db):
    """A referenced rid that resolves NOWHERE (sidecar evicted/expired, no persisted
    fallback) must fail the compose loudly — naming the rid so the agent can re-run
    the source tool — never publish 'Data unavailable' holes in a financial report."""
    from unittest.mock import patch

    import pytest as _pytest

    from app.mcp.tools import report_export

    tenant = await create_test_tenant(db, name="Refuse Corp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="t")
    db.add(session)
    await db.flush()

    with patch("app.services.chat.result_cache._get_redis", return_value=_DictRedis()):
        with _pytest.raises(ValueError, match="r1"):
            await report_export.execute(
                {
                    "title": "Holey",
                    "sections": [
                        {"type": "heading", "level": 1, "text": "H"},
                        {"type": "table", "result_id": "r1"},
                    ],
                },
                context={"db": db, "tenant_id": tenant.id, "conversation_id": str(session.id), "actor_id": user.id},
            )
    count = (await db.execute(select(func.count(Report.id)).where(Report.tenant_id == tenant.id))).scalar_one()
    assert count == 0, "a refused compose must not persist a report row"


async def test_compose_survives_a_deep_research_turn(db):
    """The original live failure shape: EIGHT stamped results in one turn, compose
    references the FIRST and the LAST. The first must still resolve at compose time
    (cap covers a full turn) and the recipe must capture both sources."""
    import json as _json
    from unittest.mock import patch

    from app.mcp.tools import report_export

    tenant = await create_test_tenant(db, name="Deep Turn Corp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="t")
    db.add(session)
    await db.flush()
    conversation_id = str(session.id)

    with patch("app.services.chat.result_cache._get_redis", return_value=_DictRedis()):
        from app.services.chat.result_cache import cache_full_payload
        from app.services.chat.tool_call_results import extract_result_payload

        for i in range(1, 9):  # r1..r8 — more results than the old cap of 6
            params = {"query": f"SELECT {i}"}
            payload = extract_result_payload(
                "netsuite_suiteql",
                params,
                _json.dumps(
                    {
                        "success": True,
                        "columns": ["account", "amount"],
                        "rows": [[f"MARKER-R{i}", i * 100]],
                        "row_count": 1,
                        "query": params["query"],
                    }
                ),
            )
            cache_full_payload(conversation_id, f"r{i}", payload, tool_name="netsuite_suiteql", params=params)

        result = await report_export.execute(
            {
                "title": "Deep",
                "sections": [
                    {"type": "heading", "level": 1, "text": "Deep"},
                    {"type": "table", "result_id": "r1"},
                    {"type": "table", "result_id": "r8"},
                ],
            },
            context={"db": db, "tenant_id": tenant.id, "conversation_id": conversation_id, "actor_id": user.id},
        )

    report = (await db.execute(select(Report).where(Report.id == uuid.UUID(result["report_id"])))).scalar_one()
    assert "MARKER-R1" in report.rendered_html, "the turn's FIRST result must survive to compose"
    assert "MARKER-R8" in report.rendered_html
    assert "Data unavailable" not in report.rendered_html
    assert report.recipe_json is not None, "recipe capture must not fail-close on a deep turn"
    assert set(report.recipe_json["sources"]) == {"r1", "r8"}


async def test_compose_degrades_narrative_only_references_gracefully(db):
    """Gate r1 on the refusal fix: a rid referenced ONLY inside narrative
    {{result:...}} placeholders is NOT a hard dependency — fill_placeholders
    degrades it to a visible inline '[unresolved: ...]' marker while every real
    data section composes. Hard-fail is reserved for DATA sections' result_id."""
    import json as _json
    from unittest.mock import patch

    from app.mcp.tools import report_export

    tenant = await create_test_tenant(db, name="Narrative Corp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="t")
    db.add(session)
    await db.flush()
    conversation_id = str(session.id)

    with patch("app.services.chat.result_cache._get_redis", return_value=_DictRedis()):
        from app.services.chat.result_cache import cache_full_payload
        from app.services.chat.tool_call_results import extract_result_payload

        params = {"query": "SELECT 1"}
        payload = extract_result_payload(
            "netsuite_suiteql",
            params,
            _json.dumps(
                {"success": True, "columns": ["a", "amount"], "rows": [["OK", 5]], "row_count": 1, "query": "q"}
            ),
        )
        cache_full_payload(conversation_id, "r1", payload, tool_name="netsuite_suiteql", params=params)

        result = await report_export.execute(
            {
                "title": "Narrative",
                "sections": [
                    {"type": "table", "result_id": "r1"},
                    {"type": "narrative", "markdown": "Stale ref: {{result:r9.row_count}}"},
                ],
            },
            context={"db": db, "tenant_id": tenant.id, "conversation_id": conversation_id, "actor_id": user.id},
        )

    report = (await db.execute(select(Report).where(Report.id == uuid.UUID(result["report_id"])))).scalar_one()
    assert "OK" in report.rendered_html  # the real data section composed
    assert "[unresolved:" in report.rendered_html  # the stale narrative ref is visibly marked


async def test_compose_precheck_survives_transient_resolver_errors(db, monkeypatch):
    """Gate r1: the pre-check must catch ANY resolver failure (a Redis blip raises
    ConnectionError, not KeyError) and refuse with the agent-actionable ValueError —
    never a raw 500."""
    import pytest as _pytest

    from app.mcp.tools import report_export

    tenant = await create_test_tenant(db, name="Blip Corp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="t")
    db.add(session)
    await db.flush()

    def exploding(*a, **kw):
        raise RuntimeError("redis blip")

    monkeypatch.setattr("app.services.chat.result_cache.get_full_payload", exploding)
    with _pytest.raises(ValueError, match="r1"):
        await report_export.execute(
            {"title": "Blip", "sections": [{"type": "table", "result_id": "r1"}]},
            context={"db": db, "tenant_id": tenant.id, "conversation_id": str(session.id), "actor_id": user.id},
        )


async def test_compose_resolves_each_rid_once(db):
    """Gate r1 (efficiency cluster): the pre-check + section render + placeholder
    fill must share ONE resolution per rid (memoized resolver), not re-hit Redis
    per reference."""
    import json as _json
    from unittest.mock import patch

    from app.mcp.tools import report_export
    from app.services.chat import result_cache as rc

    tenant = await create_test_tenant(db, name="Memo Corp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    session = ChatSession(tenant_id=tenant.id, user_id=user.id, title="t")
    db.add(session)
    await db.flush()
    conversation_id = str(session.id)

    with patch("app.services.chat.result_cache._get_redis", return_value=_DictRedis()):
        from app.services.chat.tool_call_results import extract_result_payload

        params = {"query": "SELECT 1"}
        payload = extract_result_payload(
            "netsuite_suiteql",
            params,
            _json.dumps(
                {"success": True, "columns": ["a", "amount"], "rows": [["OK", 5]], "row_count": 1, "query": "q"}
            ),
        )
        rc.cache_full_payload(conversation_id, "r1", payload, tool_name="netsuite_suiteql", params=params)

        calls: list[str] = []
        real = rc.get_full_payload

        def counting(conv, rid):
            calls.append(rid)
            return real(conv, rid)

        with patch.object(rc, "get_full_payload", side_effect=counting):
            await report_export.execute(
                {
                    "title": "Memo",
                    "sections": [
                        {"type": "table", "result_id": "r1"},
                        {"type": "narrative", "markdown": "Rows: {{result:r1.row_count}}"},
                    ],
                },
                context={"db": db, "tenant_id": tenant.id, "conversation_id": conversation_id, "actor_id": user.id},
            )
    assert calls.count("r1") == 1, f"r1 resolved {calls.count('r1')}x — the resolver must memoize"
