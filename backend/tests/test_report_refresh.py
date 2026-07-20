"""Slice B (live-dashboard reports) — the manual-refresh service.

``refresh_report`` re-executes a report's captured recipe HEADLESSLY (no LLM, no agent
loop): each source's {tool, params} replays through the real chat dispatcher seam
(monkeypatched here with canned JSON strings — the first test of the
capture→execute→extract_result_payload chain), payloads rebuild through
``extract_result_payload``, ``assemble_spec`` re-runs over the ORIGINAL sections, and
the result publishes as a NEW immutable ``report_versions`` row with the parent
mirroring the latest. Failure can never corrupt the current version; a hostile recipe
can never execute a mutation (the dispatcher has NO built-in guard — the service's
per-source ``is_recipe_eligible`` re-check is the caller-side gate).
Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md §4B/§6.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.report_version import ReportVersion
from app.services.report.playbooks import build_playbook_recipe
from app.services.report.refresh_service import (
    REFRESH_MIN_INTERVAL_SECONDS,
    RefreshDebouncedError,
    RefreshError,
    refresh_report,
)
from tests.conftest import create_test_tenant, create_test_user
from tests.fixtures import statement_fixture as fx

_SECTIONS = [
    {"type": "heading", "level": 1, "text": "Cash"},
    {"type": "table", "result_id": "r1", "label": "Cash by account"},
    {"type": "narrative", "markdown": "Rows: {{result:r1.row_count}}"},
]


def _recipe(tool="netsuite_suiteql", params=None):
    return {
        "schema_version": 1,
        "captured_at": "2026-07-06T18:00:00+00:00",
        "sections": _SECTIONS,
        "sources": {"r1": {"tool": tool, "params": params or {"query": "SELECT 1"}, "connection_id": None}},
    }


def _fresh_result_str(amount=999):
    # the SuiteQL result shape extract_result_payload Path 1 parses
    return json.dumps(
        {
            "success": True,
            "columns": ["account", "amount"],
            "rows": [["Cash", amount]],
            "row_count": 1,
            "query": "SELECT 1",
        }
    )


async def _seed_report(db, *, recipe=None, html="<html>v1</html>"):
    tenant = await create_test_tenant(db, name="RefreshCorp")
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    report = Report(
        tenant_id=tenant.id,
        title="Cash report",
        spec_json={"title": "Cash report", "sections": []},
        rendered_html=html,
        created_by=user.id,
        recipe_json=recipe,
    )
    db.add(report)
    await db.flush()
    return tenant, user, report


def _patch_executor(monkeypatch, result_str=None, calls=None, by_params=None):
    """``by_params`` (a ``{(report_type, period): result_str}`` map) serves each call the
    result matching its OWN params — needed once a recipe fans out to multiple sources
    with different report_type/period pairs (financial_statement recipes); a call whose
    params aren't in the map falls back to ``result_str``/``_fresh_result_str()``."""
    calls = calls if calls is not None else []

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        calls.append({"tool": tool_name, "params": tool_input, "tenant_id": tenant_id, "actor_id": actor_id})
        if by_params is not None:
            key = (tool_input.get("report_type"), tool_input.get("period"))
            return by_params.get(key, result_str or _fresh_result_str())
        return result_str or _fresh_result_str()

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)
    return calls


def _raw_tool_result(payload: dict) -> str:
    """A statement_fixture EXTRACTED payload (columns/rows/row_count/query) reconstructed
    as the RAW netsuite_financial_report tool-result JSON ``extract_result_payload`` Path
    1 (columns+rows) parses — mirrors ``test_report_playbooks._raw_tool_result``,
    duplicated here to keep this module's fixtures self-contained. A ``fx._failed(...)``
    payload is already in the raw shape and passes through unchanged."""
    if payload.get("success") is False:
        return json.dumps(payload)
    return json.dumps(
        {
            "success": True,
            "columns": payload["columns"],
            "rows": payload["rows"],
            "row_count": payload["row_count"],
            "query": payload.get("query", ""),
        },
        default=str,
    )


_IS_TREND_PERIOD = "Jan 2026,Feb 2026,Mar 2026,Apr 2026,May 2026,Jun 2026"


def _income_statement_by_params(*, r1=None, r2=None, r3=None, r4=None) -> dict:
    payloads = fx.income_statement_payloads()
    return {
        ("income_statement", "Jun 2026"): r1 or _raw_tool_result(payloads["r1"]),
        ("income_statement", "May 2026"): r2 or _raw_tool_result(payloads["r2"]),
        ("income_statement", "Jun 2025"): r3 or _raw_tool_result(payloads["r3"]),
        ("income_statement_trend", _IS_TREND_PERIOD): r4 or _raw_tool_result(payloads["r4"]),
    }


async def test_refresh_publishes_new_version_with_fresh_numbers(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    original_created_at = report.created_at
    calls = _patch_executor(monkeypatch, _fresh_result_str(amount=4242))

    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)

    # the source replayed with the STORED params under the report's tenant/actor
    assert calls and calls[0]["tool"] == "netsuite_suiteql"
    assert calls[0]["params"] == {"query": "SELECT 1"}
    assert calls[0]["tenant_id"] == tenant.id

    # parent mirrors the new current version
    assert updated.version == 2
    assert "4,242.00" in updated.rendered_html  # fresh number, accounting-formatted
    assert 'class="stamp"' in updated.rendered_html  # freshness honesty footer
    assert updated.last_refreshed_at is not None

    # immutable history: v1 = lazy snapshot of the pre-refresh parent, v2 = the new build
    versions = (await db.execute(select(ReportVersion).where(ReportVersion.report_id == report.id))).scalars().all()
    by_v = {v.version: v for v in versions}
    assert set(by_v) == {1, 2}
    assert by_v[1].rendered_html == "<html>v1</html>"
    assert by_v[1].created_at == original_created_at  # honest history dates
    assert by_v[2].rendered_html == updated.rendered_html

    # audited, keyed on the stable report id
    audit = (
        await db.execute(
            text("SELECT count(*) FROM audit_events WHERE action='report.refresh' AND resource_id=:rid"),
            {"rid": str(report.id)},
        )
    ).scalar()
    assert audit == 1


async def test_second_refresh_appends_v3_and_preserves_history(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch, _fresh_result_str(amount=1))
    await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    v2_html = report.rendered_html

    # step past the debounce window, then refresh again with different data
    report.last_refreshed_at = datetime.now(timezone.utc) - timedelta(seconds=REFRESH_MIN_INTERVAL_SECONDS + 1)
    await db.flush()
    _patch_executor(monkeypatch, _fresh_result_str(amount=2))
    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)

    assert updated.version == 3
    versions = (await db.execute(select(ReportVersion).where(ReportVersion.report_id == report.id))).scalars().all()
    by_v = {v.version: v for v in versions}
    assert set(by_v) == {1, 2, 3}
    assert by_v[2].rendered_html == v2_html  # prior version untouched (immutable)


async def test_refresh_within_window_debounced_429(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch)
    await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    with pytest.raises(RefreshDebouncedError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 429
    assert exc.value.retry_after_seconds > 0


async def test_snapshot_only_report_refuses_409(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=None)
    calls = _patch_executor(monkeypatch)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 409
    assert not calls  # nothing executed


async def test_missing_report_is_404(db, monkeypatch):
    tenant, user, _ = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=uuid.uuid4(), tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 404


# --- Safety + failure semantics (the load-bearing gates) ------------------------------


async def test_tampered_mutation_tool_refused_before_dispatch(db, monkeypatch):
    """The dispatcher has NO built-in mutation gate — the service's per-source
    is_recipe_eligible re-check must refuse BEFORE any execution."""
    hostile = _recipe(tool="ext__0f3c9a2e00000000000000000000beef__ns_createRecord", params={"type": "customer"})
    hostile["sources"]["r1"]["connection_id"] = "0f3c9a2e-0000-0000-0000-000000beef00"
    tenant, user, report = await _seed_report(db, recipe=hostile)
    calls = _patch_executor(monkeypatch)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 409
    assert calls == []  # NEVER dispatched


async def test_unknown_schema_version_refused(db, monkeypatch):
    recipe = _recipe()
    recipe["schema_version"] = 2
    tenant, user, report = await _seed_report(db, recipe=recipe)
    calls = _patch_executor(monkeypatch)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 409 and calls == []


async def test_ext_connection_id_mismatch_refused(db, monkeypatch):
    recipe = _recipe(tool="ext__0f3c9a2e00000000000000000000beef__ns_runReport", params={"reportId": 7})
    recipe["sources"]["r1"]["connection_id"] = str(uuid.uuid4())  # tampered — different connector
    tenant, user, report = await _seed_report(db, recipe=recipe)
    calls = _patch_executor(monkeypatch)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 409 and calls == []


async def test_source_error_fails_whole_refresh_and_never_corrupts_current(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe(), html="<html>golden</html>")
    rid, tid, uid = report.id, tenant.id, user.id  # the service's rollback expires ORM instances
    _patch_executor(monkeypatch, json.dumps({"error": True, "message": "invalid or expired token"}))
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)
    assert exc.value.status_code == 502
    assert "invalid or expired token" in exc.value.detail
    # current version untouched; no version rows created
    row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one()
    assert row.rendered_html == "<html>golden</html>" and row.version == 1
    count = (await db.execute(select(func.count(ReportVersion.id)).where(ReportVersion.report_id == rid))).scalar()
    assert count == 0
    # durable failure audit
    audit = (
        await db.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action='report.refresh' "
                "AND status='error' AND resource_id=:arid"
            ),
            {"arid": str(rid)},
        )
    ).scalar()
    assert audit == 1


async def test_source_error_key_reaches_refresh_detail(db, monkeypatch):
    """Local tools (e.g. netsuite_financial_report) fail with
    {"success": False, "error": "<real reason>"} — no message/detail keys.
    The reason must reach RefreshError.detail (and thus the failure audit row),
    not be dropped into a bare "source rN failed"."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch, json.dumps({"success": False, "error": "No active NetSuite connection found"}))
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 502
    assert "No active NetSuite connection found" in exc.value.detail


async def test_unextractable_source_result_fails_502(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch, json.dumps({"success": True}))  # parseable but no data shape
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert exc.value.status_code == 502


async def test_failed_attempt_still_consumes_debounce_window(db, monkeypatch):
    """Quota protection: hammering Refresh against a dead OAuth connection must not
    retry-storm — the stamp is attempt-time."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    rid, tid, uid = report.id, tenant.id, user.id  # the service's rollback expires ORM instances
    _patch_executor(monkeypatch, json.dumps({"error": True, "message": "dead token"}))
    with pytest.raises(RefreshError):
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)
    with pytest.raises(RefreshDebouncedError):
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)


async def test_sections_referencing_missing_source_fail_before_publish(db, monkeypatch):
    """Tampered/drifted recipe: a section rid absent from sources must 502 — never
    publish a version with 'Data unavailable' sections."""
    recipe = _recipe()
    recipe["sections"] = recipe["sections"] + [{"type": "table", "result_id": "r9"}]
    tenant, user, report = await _seed_report(db, recipe=recipe, html="<html>golden</html>")
    rid, tid, uid = report.id, tenant.id, user.id  # the service's rollback expires ORM instances
    _patch_executor(monkeypatch)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)
    assert exc.value.status_code == 502 and "r9" in exc.value.detail
    row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one()
    assert row.rendered_html == "<html>golden</html>" and row.version == 1


# --- T2-gate round-1 fixes: RLS context across commits, LLM strip, supersede guard ----
# SET LOCAL app.current_tenant_id is TRANSACTION-scoped: every commit/rollback clears it.
# The test fixture wraps tests in an outer transaction (savepoints), so the GUC survives
# test "commits" — these tests therefore assert the CALL ORDERING of set_tenant_context
# (spied in the service's namespace) against commit/rollback/dispatch events, which is
# deterministic and immune to the fixture masking.


def _spy_events(monkeypatch, db, calls=None):
    events: list = []
    real_commit, real_rollback = db.commit, db.rollback

    async def spy_ctx(session, tenant_id):
        from sqlalchemy import text as _text

        events.append("ctx")
        await session.execute(_text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))

    async def spy_commit():
        events.append("commit")
        await real_commit()

    async def spy_rollback():
        events.append("rollback")
        await real_rollback()

    monkeypatch.setattr("app.services.report.refresh_service.set_tenant_context", spy_ctx)
    monkeypatch.setattr(db, "commit", spy_commit)
    monkeypatch.setattr(db, "rollback", spy_rollback)
    return events


async def test_tenant_context_reestablished_after_claim_commit_before_dispatch(db, monkeypatch):
    """Blocker fix: Phase 1's commit clears the GUC; Phase 2's dispatch (and every
    subsequent source) must run under a re-established tenant context."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    events = _spy_events(monkeypatch, db)

    calls: list = []

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        events.append("exec")
        calls.append(tool_name)
        return _fresh_result_str()

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)
    await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)

    first_commit = events.index("commit")
    first_exec = events.index("exec")
    assert first_commit < first_exec, "claim must commit before any tool runs"
    assert "ctx" in events[first_commit:first_exec], "context must be re-set between the claim commit and dispatch"
    # after the FINAL commit, db.refresh(report) re-selects the row — context again
    last_commit = len(events) - 1 - events[::-1].index("commit")
    assert "ctx" in events[last_commit:], "context must be re-set after the publish commit (db.refresh reads the row)"


async def test_failure_audit_runs_under_reestablished_context(db, monkeypatch):
    """Major fix: the failure path rolls back (clearing the GUC) then writes the
    failure audit — a context re-set must sit between rollback and that write."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    rid, tid, uid = report.id, tenant.id, user.id
    events = _spy_events(monkeypatch, db)

    async def failing_execute(*a, **kw):
        events.append("exec")
        return json.dumps({"error": True, "message": "dead token"})

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", failing_execute)
    with pytest.raises(RefreshError):
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)
    assert "rollback" in events
    rb = events.index("rollback")
    assert "ctx" in events[rb:], "failure audit must run under a re-established tenant context"


async def test_replay_strips_llm_judge_params(db, monkeypatch):
    """Major fix (§5 no-LLM invariant): a captured suiteql `user_question` param would
    re-trigger the judge LLM on every refresh — replay must strip it."""
    recipe = _recipe(params={"query": "SELECT 1", "user_question": "how is my cash?"})
    tenant, user, report = await _seed_report(db, recipe=recipe)
    calls = _patch_executor(monkeypatch)
    await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert calls[0]["params"] == {"query": "SELECT 1"}  # user_question stripped, query intact


async def test_only_referenced_sources_are_dispatched(db, monkeypatch):
    """A tampered/drifted recipe with an extra unreferenced source must not burn a
    tool call on it — only the rids the sections reference execute."""
    recipe = _recipe()
    recipe["sources"]["r9"] = {"tool": "netsuite_suiteql", "params": {"query": "SELECT 9"}, "connection_id": None}
    tenant, user, report = await _seed_report(db, recipe=recipe)
    calls = _patch_executor(monkeypatch)
    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert updated.version == 2
    assert [c["params"] for c in calls] == [{"query": "SELECT 1"}]  # r9 never dispatched


# --- Slice C: system-actor plumbing (the Beat sweep refreshes with no human actor) ----


async def _refresh_audit_actor(db, report_id, *, status="success"):
    return (
        await db.execute(
            text(
                "SELECT actor_id, actor_type FROM audit_events "
                "WHERE action='report.refresh' AND status=:st AND resource_id=:rid"
            ),
            {"st": status, "rid": str(report_id)},
        )
    ).first()


async def test_system_actor_refresh_publishes_with_null_created_by(db, monkeypatch):
    """The sweep calls refresh_report(actor_id=None, actor_type="system") — the new
    version's author is NULL (no human) and the audit event carries the system actor."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch)

    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=None, actor_type="system")

    assert updated.version == 2
    v2 = (
        await db.execute(select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.version == 2))
    ).scalar_one()
    assert v2.created_by is None
    row = await _refresh_audit_actor(db, report.id)
    assert row is not None
    assert row[0] is None and row[1] == "system"


async def test_system_actor_failure_audit_carries_system_actor(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    rid, tid = report.id, tenant.id  # the service's rollback expires ORM instances
    _patch_executor(monkeypatch, json.dumps({"error": True, "message": "dead token"}))
    with pytest.raises(RefreshError):
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=None, actor_type="system")
    row = await _refresh_audit_actor(db, rid, status="error")
    assert row is not None
    assert row[0] is None and row[1] == "system"


async def test_default_actor_type_stays_user(db, monkeypatch):
    """The HTTP path is unchanged: actor_id=user.id with no actor_type kwarg keeps
    authoring + auditing as that user."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch)
    await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    v2 = (
        await db.execute(select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.version == 2))
    ).scalar_one()
    assert v2.created_by == user.id
    row = await _refresh_audit_actor(db, report.id)
    assert row is not None
    assert row[0] == user.id and row[1] == "user"


async def test_system_refresh_threads_actor_type_into_local_tool_dispatch(db, monkeypatch):
    """Gate r1 finding: the per-tool-call audit rows of a system sweep were stamped
    actor_type='user' with actor_id NULL — indistinguishable from a lost user id.
    The caller's actor_type must thread refresh_report → execute_tool_call →
    mcp_server.call_tool (the governance layer stamps audit rows from it)."""
    from app.services.chat import tools as chat_tools

    tenant, user, report = await _seed_report(db, recipe=_recipe())
    captured: dict = {}

    async def fake_call_tool(**kw):
        captured.update(kw)
        return json.loads(_fresh_result_str())

    monkeypatch.setattr(chat_tools.mcp_server, "call_tool", fake_call_tool)
    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=None, actor_type="system")
    assert updated.version == 2
    assert captured.get("actor_id") is None
    assert captured.get("actor_type") == "system"


async def test_governed_execute_audits_with_caller_actor_type(db):
    """The deepest link of the system-actor chain: governed_execute's tool_call audit
    rows must carry the CALLER's actor_type, not a hardcoded 'user'."""
    from app.mcp.governance import governed_execute

    tenant, _, _ = await _seed_report(db, recipe=None)

    async def fake_tool(params, context=None):
        return {"success": True}

    result = await governed_execute(
        tool_name="test.slice_c_probe",  # no TOOL_CONFIGS entry → params pass through
        params={"query": "SELECT 1"},
        tenant_id=str(tenant.id),
        actor_id=None,
        execute_fn=fake_tool,
        db=db,
        actor_type="system",
    )
    assert result == {"success": True}
    row = (
        await db.execute(
            text(
                "SELECT actor_id, actor_type FROM audit_events "
                "WHERE category='tool_call' AND action='tool.executed' AND tenant_id=:tid"
            ),
            {"tid": str(tenant.id)},
        )
    ).first()
    assert row is not None, "tool.executed audit row missing"
    assert row[0] is None and row[1] == "system"


async def test_refresh_report_refuses_anonymous_user_actor(db, monkeypatch):
    """Gate r1 finding: nothing tied actor_id=None to actor_type='system' — a caller
    forgetting actor_type would write user-attributed audit rows with no user id.
    Fail loudly at the seam instead."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    calls = _patch_executor(monkeypatch)
    with pytest.raises(ValueError, match="actor_type"):
        await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=None)
    assert calls == []  # refused before any claim or dispatch


async def test_execute_tool_call_maps_none_actor_to_none_not_str(db, monkeypatch):
    """Load-bearing for the system-actor sweep: tools.py stringifies actor_id before the
    local dispatch, and str(None) == "None" is TRUTHY — governance's
    `uuid.UUID(actor_id) if actor_id else None` would raise on it, failing every local
    source in a system refresh. None must reach the MCP server as None."""
    from app.services.chat import tools as chat_tools

    captured: dict = {}

    async def fake_call_tool(**kw):
        captured.update(kw)
        return {"success": True}

    monkeypatch.setattr(chat_tools.mcp_server, "call_tool", fake_call_tool)
    assert "netsuite_suiteql" in chat_tools._LOCAL_NAME_MAP  # the canonical recipe tool
    await chat_tools.execute_tool_call(
        "netsuite_suiteql", {"query": "SELECT 1"}, tenant_id=uuid.uuid4(), actor_id=None, correlation_id="c", db=db
    )
    assert "actor_id" in captured
    assert captured["actor_id"] is None, f"None actor must not stringify (got {captured['actor_id']!r})"


async def test_superseded_refresh_aborts_before_publish(db, monkeypatch):
    """Major fix: a slow refresh overtaken by a newer claim (window expired mid-flight)
    must NOT publish stale data over the newer version — compare-and-publish guard."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    rid, tid, uid = report.id, tenant.id, user.id

    async def overtaking_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        # Simulate a competing refresh claiming the window while we execute — via RAW SQL,
        # exactly like a concurrent request's committed write: it must NOT go through this
        # session's identity map, or the Phase-3 re-read could echo our own cached instance
        # instead of the database row (the re-gate's dead-guard finding).
        await db.execute(
            text("UPDATE reports SET last_refreshed_at = :ts WHERE id = :rid"),
            {"ts": datetime.now(timezone.utc) + timedelta(seconds=5), "rid": rid},
        )
        return _fresh_result_str()

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", overtaking_execute)
    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)
    assert exc.value.status_code == 409
    count = (await db.execute(select(func.count(ReportVersion.id)).where(ReportVersion.report_id == rid))).scalar()
    assert count == 0  # nothing published over the newer claim


# --- Provenance (Task 6): refresh renders a human-readable "Sources & method" block --


async def test_refresh_embeds_provenance_block(db, monkeypatch):
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    _patch_executor(monkeypatch, _fresh_result_str())
    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)
    assert "Sources" in updated.rendered_html and "netsuite_suiteql" not in updated.rendered_html
    assert "NetSuite SuiteQL query" in updated.rendered_html


# --- Task 4: financial_statement recipes through the refresh engine --------------------


async def test_refresh_of_statement_recipe_reassembles_full_statement(db, monkeypatch):
    """A playbook-captured financial_statement recipe refreshes through the SAME engine
    as a v1 recipe: fresh payloads re-execute, assemble_spec rebuilds the statement
    model, and the new version's rendered_html carries the KPI/quad/statement/provenance
    just like the initial compose."""
    _title, recipe = build_playbook_recipe("income_statement", {"period": "Jun 2026"})
    tenant, user, report = await _seed_report(db, recipe=recipe)
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)

    assert len(calls) == 4
    html = updated.rendered_html
    assert html.count("<h1") == 1
    assert "Net income" in html
    assert 'class="fs-quad' in html
    for rid in ("r1", "r2", "r3", "r4"):
        assert f"{rid} —" in html
    assert json.dumps(updated.spec_json)  # Risk 3: JSON-clean, no raw Decimal survived
    model = next(s["model"] for s in updated.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["prior_period"] == "May 2026"
    assert model["trend"]["periods"] == fx.EXPECTED_TREND_PERIODS


async def test_refresh_of_statement_recipe_degrades_when_compare_sources_fail(db, monkeypatch):
    """Risk 2 on the refresh path: r1 succeeds, r2/r3/r4 all fail — the refresh still
    publishes a new version (never fails closed on a compare-source outage), just
    without the deltas/YoY/trend those sources feed."""
    _title, recipe = build_playbook_recipe("income_statement", {"period": "Jun 2026"})
    tenant, user, report = await _seed_report(db, recipe=recipe)
    failed = json.dumps({"success": False, "error": "No active NetSuite connection found"})
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params(r2=failed, r3=failed, r4=failed))

    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)

    assert len(calls) == 4  # every source still attempted
    assert updated.version == 2  # the refresh PUBLISHED — it did not fail closed
    model = next(s["model"] for s in updated.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["prior_period"] is None
    assert model["trend"] is None
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == "$13,500,000"
    assert kpis["revenue"]["mom_delta"] is None


async def test_refresh_of_statement_recipe_r1_failure_still_fails_closed(db, monkeypatch):
    """Risk 2's other half on the refresh path: r1 (current period) is still a hard
    dependency — its failure raises RefreshError and never corrupts the current
    version, exactly like the pre-Task-4 fail-closed semantics for every other rid."""
    _title, recipe = build_playbook_recipe("income_statement", {"period": "Jun 2026"})
    tenant, user, report = await _seed_report(db, recipe=recipe)
    rid, tid, uid, original_html = report.id, tenant.id, user.id, report.rendered_html
    failed = json.dumps({"success": False, "error": "No active NetSuite connection found"})
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params(r1=failed))

    with pytest.raises(RefreshError) as exc:
        await refresh_report(db, report_id=rid, tenant_id=tid, actor_id=uid)
    assert "No active NetSuite connection found" in exc.value.detail
    assert calls and calls[0]["params"]["report_type"] == "income_statement"
    row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one()
    assert row.rendered_html == original_html  # current version untouched


async def test_refresh_of_v1_style_recipe_stays_byte_stable_no_fs_markup(db, monkeypatch):
    """v1 (table/narrative) recipes must refresh exactly as before Task 4's
    financial_statement wiring landed: no fs-* CSS/markup leaks into a plain report, and
    spec_json stays trivially JSON-safe (no financial_statement section to sanitize)."""
    tenant, user, report = await _seed_report(db, recipe=_recipe())
    calls = _patch_executor(monkeypatch, _fresh_result_str(amount=777))

    updated = await refresh_report(db, report_id=report.id, tenant_id=tenant.id, actor_id=user.id)

    assert len(calls) == 1
    assert "777.00" in updated.rendered_html
    assert "fs-kpi" not in updated.rendered_html
    assert "fs-quad" not in updated.rendered_html
    assert not any(
        isinstance(s, dict) and s.get("type") == "financial_statement" for s in updated.spec_json["sections"]
    )
    assert json.dumps(updated.spec_json)
