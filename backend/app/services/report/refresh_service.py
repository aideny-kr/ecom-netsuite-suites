"""Manual report refresh — Slice B of live-dashboard reports.

Re-executes a report's captured recipe HEADLESSLY (no LLM, no agent loop — §5
no-LLM-numbers) and publishes the result as a NEW immutable ``report_versions`` row,
with the parent ``reports`` row mirroring the latest (the stable identity/URL never
changes). Three phases so a failure can never corrupt the current version:

1. **Claim (own txn, committed before any tool runs):** RLS context → ``FOR UPDATE``
   row lock → recipe eligibility checks → debounce → stamp ``last_refreshed_at``.
   The stamp is ATTEMPT-time, deliberately: a failing refresh (the known dead-OAuth
   single-use-token mode) still consumes the ~5 min window, so hammering Refresh can
   never burn tenant NetSuite quota in a retry storm. (Alternative — stamp on success
   — rejected for exactly that reason.)
2. **Headless re-execution (no report writes):** each source replays through the real
   chat dispatcher (``execute_tool_call``) with the STORED params under the report's
   tenant. SECURITY: the dispatcher has NO built-in mutation/HITL gate (in chat that
   lives in base_agent) — this service's per-source ``is_recipe_eligible`` re-check is
   the load-bearing caller-side gate against a tampered ``recipe_json`` row, on top of
   capture-time structural exclusion, per-tool read-only validators, and the
   connector-consistency check. Any source failure fails the WHOLE refresh — never a
   version with mixed-vintage or missing numbers (coverage of every referenced rid is
   verified BEFORE assemble_spec, which would otherwise render "Data unavailable"
   sections instead of raising).
3. **Atomic publish (own txn):** lazy v1 snapshot on first refresh (pre-refresh parent
   state, honest history dates) → insert version N+1 → mirror parent → audit
   ``report.refresh`` → commit. Unlike compose (chat-turn atomicity), this is a normal
   FastAPI request: the service commits.

Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md §4B/§5/§6.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.report_version import ReportVersion
from app.services import audit_service
from app.services.report.recipe import is_recipe_eligible

logger = logging.getLogger(__name__)

REFRESH_MIN_INTERVAL_SECONDS = 300  # spec §6.3 "~5 min" per-report debounce
MAX_RECIPE_SOURCES = 12  # cheap guard against absurd synchronous fan-out (risk §8.4)


class RefreshError(Exception):
    """Clean-failure carrier: maps 1:1 onto the HTTP error the endpoint raises."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RefreshDebouncedError(RefreshError):
    def __init__(self, retry_after_seconds: int):
        super().__init__(429, f"refreshed recently — try again in about {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


async def _locked_report(db: AsyncSession, report_id: uuid.UUID) -> Report:
    # populate_existing is LOAD-BEARING (T2 re-gate): without it the identity map hands
    # back this session's cached instance UNREFRESHED, so Phase 3's supersede comparison
    # would echo our own in-memory write and never observe a concurrent request's
    # committed stamp — the FOR UPDATE row read must reflect the database row.
    row = (
        await db.execute(
            select(Report).where(Report.id == report_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:  # RLS makes cross-tenant rows invisible — same 404 shape as the API
        raise RefreshError(404, "report not found")
    return row


def _validated_sources(recipe: dict | None) -> dict[str, dict]:
    """Eligibility checks on the stored recipe — fail closed, refuse to guess."""
    if not isinstance(recipe, dict) or recipe.get("sources") is None:
        raise RefreshError(409, "snapshot-only report — no refresh recipe")
    if recipe.get("schema_version") != 1:
        raise RefreshError(409, "unsupported recipe schema — recompose the report to refresh it")
    sources = recipe.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise RefreshError(409, "recipe has no sources — snapshot-only report")
    if len(sources) > MAX_RECIPE_SOURCES:
        raise RefreshError(422, f"recipe references too many sources ({len(sources)} > {MAX_RECIPE_SOURCES})")
    if not isinstance(recipe.get("sections"), list) or not recipe["sections"]:
        raise RefreshError(409, "recipe has no sections — recompose the report to refresh it")
    return sources


def _check_source(rid: str, src: object) -> tuple[str, dict]:
    """Per-source trust boundary (load-bearing — see module docstring)."""
    from app.services.chat.tools import parse_external_tool_name

    if not isinstance(src, dict) or not isinstance(src.get("tool"), str) or not isinstance(src.get("params"), dict):
        raise RefreshError(409, f"source {rid} is malformed — recompose the report to refresh it")
    tool, params = src["tool"], src["params"]
    if not is_recipe_eligible(tool):
        # a legitimately captured recipe can never contain this — treat as tampered
        raise RefreshError(409, f"source {rid} tool is not refresh-eligible")
    parsed = parse_external_tool_name(tool)
    stored_conn = src.get("connection_id")
    if parsed is not None and stored_conn != str(parsed[0]):
        raise RefreshError(409, f"source {rid} connection mismatch — recompose the report to refresh it")
    if parsed is None and stored_conn is not None:
        raise RefreshError(409, f"source {rid} connection mismatch — recompose the report to refresh it")
    return tool, params


# Params that trigger an LLM inside a tool (§5 no-LLM-in-refresh): a captured suiteql
# `user_question` re-runs the judge model on every replay — strip at dispatch, keyed per
# tool so a legitimate same-named param on another tool is never touched.
_LLM_ONLY_PARAMS: dict[str, frozenset[str]] = {
    "netsuite_suiteql": frozenset({"user_question"}),
    "netsuite.suiteql": frozenset({"user_question"}),
}


async def _execute_sources(
    db: AsyncSession,
    sources: dict[str, dict],
    needed_rids: list[str],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    correlation_id: str,
) -> dict[str, dict]:
    """Replay ONLY the sources the sections actually reference (an extra source in a
    tampered/drifted recipe never burns a tool call); ANY failure fails the refresh.
    A referenced rid with no source fails BEFORE anything else executes downstream —
    assemble_spec would otherwise degrade the miss into "Data unavailable" sections."""
    from app.services.chat.tool_call_results import extract_result_payload
    from app.services.chat.tools import execute_tool_call

    payloads: dict[str, dict] = {}
    for rid in needed_rids:
        src = sources.get(rid)
        if src is None:
            raise RefreshError(502, f"recipe sources missing for {rid} — recompose the report")
        tool, params = _check_source(rid, src)
        strip = _LLM_ONLY_PARAMS.get(tool, frozenset())
        if strip:
            params = {k: v for k, v in params.items() if k not in strip}
        # SET LOCAL tenant context is TRANSACTION-scoped and any commit (the claim, or a
        # tool's own) clears it — re-establish before EVERY dispatch so RLS-scoped reads
        # inside the tool (e.g. the Connection lookup) always run under this tenant.
        await set_tenant_context(db, str(tenant_id))
        result_str = await execute_tool_call(
            tool, params, tenant_id=tenant_id, actor_id=actor_id, correlation_id=correlation_id, db=db
        )
        try:
            parsed = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            raise RefreshError(502, f"source {rid} ({tool}) returned an unreadable result") from None
        if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("success") is False):
            message = str(parsed.get("message") or parsed.get("detail") or parsed.get("error_message") or "")
            raise RefreshError(502, f"source {rid} ({tool}) failed{': ' + message[:200] if message else ''}")
        payload = extract_result_payload(tool, params, result_str)
        if payload is None:
            raise RefreshError(502, f"source {rid} ({tool}) returned no extractable data")
        payloads[rid] = payload
    return payloads


async def refresh_report(
    db: AsyncSession,
    *,
    report_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> Report:
    """Re-execute ``report_id``'s recipe and publish version N+1. Raises RefreshError
    (→ clean HTTP error); the current version is never corrupted on failure."""
    from app.services.report.report_html import render_report_html
    from app.services.report.report_service import assemble_spec, referenced_result_ids

    # ---- Phase 1: claim (debounce stamp committed before any tool runs) -------------
    await set_tenant_context(db, str(tenant_id))
    report = await _locked_report(db, report_id)
    recipe = report.recipe_json
    sources = _validated_sources(recipe)
    now = datetime.now(timezone.utc)
    if report.last_refreshed_at is not None:
        elapsed = (now - report.last_refreshed_at).total_seconds()
        if elapsed < REFRESH_MIN_INTERVAL_SECONDS:
            raise RefreshDebouncedError(int(REFRESH_MIN_INTERVAL_SECONDS - elapsed) + 1)
    # Snapshot the pre-refresh state in memory (for the lazy v1 row) BEFORE committing.
    pre = {
        "version": report.version,
        "spec_json": report.spec_json,
        "rendered_html": report.rendered_html,
        "created_by": report.created_by,
        "created_at": report.created_at,
    }
    report.last_refreshed_at = now
    await db.commit()

    correlation_id = f"report-refresh:{report_id}:{uuid.uuid4().hex[:8]}"
    try:
        # ---- Phase 2: headless re-execution (no report writes) ----------------------
        # Only the rids the ORIGINAL sections reference are dispatched; a referenced rid
        # without a source fails closed inside _execute_sources (never "Data unavailable").
        needed_rids = referenced_result_ids(recipe["sections"])
        payloads = await _execute_sources(
            db, sources, needed_rids, tenant_id=tenant_id, actor_id=actor_id, correlation_id=correlation_id
        )

        spec = assemble_spec(report.title, recipe["sections"], lambda rid: payloads[rid])
        html = render_report_html(
            spec,
            freshness={"composed_at": recipe.get("captured_at", ""), "refreshed_at": now.isoformat()},
        )

        # ---- Phase 3: atomic publish -------------------------------------------------
        await set_tenant_context(db, str(tenant_id))  # fresh txn after the claim commit
        report = await _locked_report(db, report_id)
        # Compare-and-publish (supersede guard): the claim's FOR UPDATE lock died at the
        # Phase-1 commit, so a slow refresh can be overtaken once the window expires. If
        # the stamp is no longer OURS, a newer refresh claimed after us — abort rather
        # than publish our (older) data over a newer version.
        if report.last_refreshed_at != now:
            raise RefreshError(409, "superseded by a newer refresh — reload to see the latest version")
        max_version = (
            await db.execute(select(func.max(ReportVersion.version)).where(ReportVersion.report_id == report_id))
        ).scalar()
        if max_version is None:
            # first refresh: lazy v1 snapshot of the pre-refresh parent (honest dates)
            db.add(
                ReportVersion(
                    tenant_id=tenant_id,
                    report_id=report_id,
                    version=pre["version"],
                    spec_json=pre["spec_json"],
                    rendered_html=pre["rendered_html"],
                    created_by=pre["created_by"],
                    created_at=pre["created_at"],
                )
            )
            await db.flush()
        next_version = (max_version or pre["version"]) + 1
        db.add(
            ReportVersion(
                tenant_id=tenant_id,
                report_id=report_id,
                version=next_version,
                spec_json=spec,
                rendered_html=html,
                created_by=actor_id,
            )
        )
        report.spec_json = spec
        report.rendered_html = html
        report.version = next_version
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="report",
            action="report.refresh",
            actor_id=actor_id,
            resource_type="report",
            resource_id=str(report_id),
            correlation_id=correlation_id,
            payload={"version": next_version, "source_count": len(sources)},
        )
        await db.commit()
        # the publish commit cleared the GUC; db.refresh re-SELECTs the row under RLS
        await set_tenant_context(db, str(tenant_id))
        await db.refresh(report)
        return report
    except Exception as exc:
        await db.rollback()
        detail = exc.detail if isinstance(exc, RefreshError) else "refresh failed"
        try:  # durable failure record in a fresh mini-txn (best-effort)
            # rollback cleared the GUC — the audit INSERT's RLS WITH CHECK needs it
            await set_tenant_context(db, str(tenant_id))
            await audit_service.log_event(
                db=db,
                tenant_id=tenant_id,
                category="report",
                action="report.refresh",
                actor_id=actor_id,
                resource_type="report",
                resource_id=str(report_id),
                correlation_id=correlation_id,
                status="error",
                error_message=str(exc)[:500],
            )
            await db.commit()
        except Exception:
            logger.warning("report.refresh failure-audit write failed", exc_info=True)
        if isinstance(exc, RefreshError):
            raise
        logger.warning("report.refresh unexpected failure", exc_info=True)
        raise RefreshError(500, detail) from exc
