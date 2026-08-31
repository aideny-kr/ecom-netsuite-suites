"""Rolling period Stage 2 — the scheduled compose that makes the wall actually roll.

Stage 1 shipped the following: a ``ReportSeries`` tracks a playbook and the dashboard
wall shows that series' NEWEST report. Nothing created the next period's report, so the
wall only moved when a human composed one by hand. ``sweep_tenant_series`` is that
missing piece.

House pattern (test_report_auto_refresh.py, test_recon_scheduled_run_all.py): the Celery
glue is tested separately; these tests call the inner async function directly with the
``db`` fixture.

The contract under test is mostly the TERMINATION REASON, because
`.claude/rules/agent-graph.md` #5 makes it law for anything a schedule starts: "Terminate
with a reason, not a boolean -- done | budget | stall | error, written to
jobs.result_summary. 'Stopped' merges 'finished' with 'stuck', and a human cannot route
what they cannot distinguish." No existing task in this repo actually does this, so these
tests are where it starts.
"""

from __future__ import annotations

from datetime import date

import pytest_asyncio

from app.models.report import Report
from app.models.report_series import ReportSeries
from app.services.report.period_resolver import ClosedPeriod, PeriodUnavailableReason
from app.services.report.refresh_service import RefreshError
from app.workers.tasks.rolling_period_compose import (
    REASON_BUDGET,
    REASON_ERROR,
    sweep_tenant_series,
)
from tests.conftest import create_test_tenant, create_test_user

_JUN_CLOSED = ClosedPeriod(name="Jun 2026", enddate=date(2026, 6, 30))


@pytest_asyncio.fixture
async def tenant_user(db):
    tenant = await create_test_tenant(db, name="SweepCorp")
    user, _ = await create_test_user(db, tenant)
    return tenant, user


def _patch_resolver(monkeypatch, closed: ClosedPeriod):
    """Patched at the period_resolver module boundary — the sweep imports it lazily,
    so a `from X import Y` at call time reads the CURRENT X.Y."""

    async def fake_resolve(db, tenant_id):
        return closed

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period", fake_resolve)
    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period_cached", fake_resolve)


def _patch_compose(monkeypatch, behaviour):
    """Replace compose_playbook_report. `behaviour` receives the playbook_key and either
    returns a Report-ish object or raises. Patched at the playbooks module boundary."""
    calls = []

    async def fake_compose(
        db,
        *,
        playbook_key,
        params,
        tenant_id,
        actor_id,
        mode="period",
        actor_type="user",
        closed_period=None,
    ):
        calls.append(
            {
                "playbook_key": playbook_key,
                "mode": mode,
                "actor_id": actor_id,
                "actor_type": actor_type,
                "closed_period": closed_period,
            }
        )
        return await behaviour(playbook_key)

    monkeypatch.setattr("app.services.report.playbooks.compose_playbook_report", fake_compose)
    return calls


async def _series(db, tenant, user, playbook_key, *, newest_period=None):
    s = ReportSeries(tenant_id=tenant.id, playbook_key=playbook_key, created_by=user.id)
    db.add(s)
    await db.flush()
    if newest_period is not None:
        db.add(
            Report(
                tenant_id=tenant.id,
                title=f"{playbook_key} {newest_period}",
                spec_json={},
                rendered_html="<html></html>",
                created_by=user.id,
                recipe_json={},
                period=newest_period,
                series_id=s.id,
            )
        )
        await db.flush()
    return s


# --- the happy path ----------------------------------------------------------------


async def test_composes_a_series_that_is_behind_the_closed_period(db, monkeypatch, tenant_user):
    tenant, user = tenant_user
    await _series(db, tenant, user, "income_statement", newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def ok(_key):
        return object()

    calls = _patch_compose(monkeypatch, ok)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "done"
    assert result["composed"] == 1
    assert result["already_current"] == 0
    assert len(calls) == 1
    assert calls[0]["mode"] == "tracking"
    # T2 gate round 1 (cost): the sweep resolves the period ONCE per tenant and hands it
    # down. Without this the per-series compose re-resolves the same answer at 2 NetSuite
    # round trips each, silently undoing the saving the docstring claims.
    assert calls[0]["closed_period"] is not None
    assert calls[0]["closed_period"].name == "Jun 2026"


async def test_skips_a_series_already_on_the_closed_period_without_composing(db, monkeypatch, tenant_user):
    """The cheap filter. agent-graph.md #14: 'Filter cheap (hash) before comparing
    expensive (model call).' Here the cheap filter is a period-string comparison against
    a row we already loaded; the expensive thing is 6 NetSuite round trips."""
    tenant, user = tenant_user
    await _series(db, tenant, user, "income_statement", newest_period="Jun 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def boom(_key):
        raise AssertionError("must not compose a series that is already current")

    _patch_compose(monkeypatch, boom)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "done"
    assert result["composed"] == 0
    assert result["already_current"] == 1


async def test_composes_an_empty_series_that_has_no_report_yet(db, monkeypatch, tenant_user):
    tenant, user = tenant_user
    await _series(db, tenant, user, "balance_sheet", newest_period=None)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def ok(_key):
        return object()

    calls = _patch_compose(monkeypatch, ok)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "done"
    assert result["composed"] == 1
    assert len(calls) == 1


# --- the reason enum ---------------------------------------------------------------


async def test_unresolvable_period_terminates_with_error_and_composes_nothing(db, monkeypatch, tenant_user):
    """NetSuite unreachable: the run cannot do its job at all. That is `error`, not
    `done` — and critically it must not be reported as success just because it made no
    changes. Absence of work is not evidence of completion."""
    tenant, user = tenant_user
    await _series(db, tenant, user, "income_statement", newest_period="May 2026")
    _patch_resolver(monkeypatch, ClosedPeriod(name=None, enddate=None, reason=PeriodUnavailableReason.UNREACHABLE))

    async def boom(_key):
        raise AssertionError("must not compose when the period is unknown")

    _patch_compose(monkeypatch, boom)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "error"
    assert result["composed"] == 0
    assert result["detail"]
    assert "unreachable" in result["detail"].lower()


async def test_hitting_the_per_run_cap_terminates_with_budget_not_done(db, monkeypatch, tenant_user):
    """agent-graph.md #6: 'Bound cost, not just call count.' One compose is 4-6 NetSuite
    round trips against a 120/min per-tenant limit SHARED with live chat, so an unbounded
    sweep can starve real users. Work remaining must surface as `budget`, never `done` —
    `done` would tell the next run there is nothing left."""
    tenant, user = tenant_user
    for key in ("income_statement", "balance_sheet", "trial_balance"):
        await _series(db, tenant, user, key, newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def ok(_key):
        return object()

    calls = _patch_compose(monkeypatch, ok)
    result = await sweep_tenant_series(db, tenant.id, max_composes=2)

    assert result["reason"] == "budget"
    assert result["composed"] == 2
    assert len(calls) == 2, "must stop dispatching once the cap is reached"
    assert result["remaining"] == 1


async def test_every_compose_failing_the_same_way_terminates_with_stall_not_error(db, monkeypatch, tenant_user):
    """~/.claude/CLAUDE.md: 'Detect stalls by fingerprinting the failure set. Identical
    failures across attempts means re-running will not help; that is different from
    converging slowly.' A stall is separately routable from a one-off error, which is the
    entire point of the enum."""
    tenant, user = tenant_user
    for key in ("income_statement", "balance_sheet"):
        await _series(db, tenant, user, key, newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def always_same_failure(_key):
        raise RefreshError(502, "NetSuite returned no rows for r1")

    _patch_compose(monkeypatch, always_same_failure)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "stall"
    assert result["failed"] == 2
    assert result["composed"] == 0


async def test_one_failure_among_successes_still_completes_the_sweep(db, monkeypatch, tenant_user):
    """Per-series isolation: one bad series must not abort the others, and a partial
    success is `done` (the sweep reached every series), with the failure counted."""
    tenant, user = tenant_user
    for key in ("income_statement", "balance_sheet"):
        await _series(db, tenant, user, key, newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def one_bad(key):
        if key == "income_statement":
            raise RefreshError(502, "transient")
        return object()

    _patch_compose(monkeypatch, one_bad)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "done"
    assert result["composed"] == 1
    assert result["failed"] == 1


async def test_a_tenant_with_no_series_is_done_not_error(db, monkeypatch, tenant_user):
    tenant, _user = tenant_user
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    _patch_compose(monkeypatch, lambda _k: None)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "done"
    assert result["series_total"] == 0


# --- audit integrity ---------------------------------------------------------------


async def test_scheduled_compose_is_attributed_to_the_system_not_a_user(db, monkeypatch, tenant_user):
    """compose_playbook_report hardcoded actor_type="user" for its only caller (an HTTP
    endpoint with a real person behind it). A scheduled compose has no person, and an
    audit trail claiming one is a false record — the same class of untruth as the Stage 1
    launcher copy that promised a scheduler which did not exist. refresh_report already
    threads actor_type="system" for exactly this reason."""
    tenant, user = tenant_user
    await _series(db, tenant, user, "income_statement", newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def ok(_key):
        return object()

    calls = _patch_compose(monkeypatch, ok)
    await sweep_tenant_series(db, tenant.id)

    assert calls[0]["actor_type"] == "system"
    assert calls[0]["actor_id"] is None


# --- tenant isolation --------------------------------------------------------------


async def test_sweep_never_touches_another_tenants_series(db, monkeypatch):
    tenant_a = await create_test_tenant(db, name="SweepAlpha")
    tenant_b = await create_test_tenant(db, name="SweepBeta")
    user_a, _ = await create_test_user(db, tenant_a)
    user_b, _ = await create_test_user(db, tenant_b)
    await _series(db, tenant_a, user_a, "income_statement", newest_period="May 2026")
    await _series(db, tenant_b, user_b, "income_statement", newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    seen = []

    async def ok(key):
        seen.append(key)
        return object()

    calls = _patch_compose(monkeypatch, ok)
    result = await sweep_tenant_series(db, tenant_a.id)

    assert result["series_total"] == 1, "must only see tenant A's series"
    assert len(calls) == 1


# --- the Beat fan-out --------------------------------------------------------------


async def test_fanout_is_disabled_by_default_and_dispatches_nothing(db, monkeypatch):
    """Fail-closed. A deployment that has never heard of this feature must sweep no one
    on first boot — the setting defaults to False, matching REPORT_AUTO_REFRESH_ENABLED."""
    from app.workers.tasks import rolling_period_compose as mod

    sent = []
    monkeypatch.setattr(mod.celery_app, "send_task", lambda *a, **k: sent.append((a, k)))
    result = await mod.collect_and_dispatch(db)

    assert result["enabled"] is False
    assert result["dispatched"] == 0
    assert sent == []


async def test_fanout_dispatches_tenant_id_as_a_kwarg_not_positionally(db, monkeypatch):
    """InstrumentedTask.before_start/on_success read kwargs.get("tenant_id")
    (base_task.py:57,90) to decide the Job row's tenant, falling back to
    SYSTEM_TENANT_ID. A positional dispatch silently files every per-tenant run under the
    system tenant, so a failure can no longer be attributed to the tenant it happened to.
    The existing precedent (report_auto_refresh.py:198) dispatches by kwargs for exactly
    this reason."""
    from app.workers.tasks import rolling_period_compose as mod

    tenant = await create_test_tenant(db, name="FanoutCorp")
    monkeypatch.setattr(mod.settings, "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED", True)

    sent = []
    monkeypatch.setattr(mod.celery_app, "send_task", lambda *a, **k: sent.append((a, k)))
    result = await mod.collect_and_dispatch(db)

    assert result["dispatched"] >= 1
    assert sent, "expected at least one dispatch"
    names = [a[0] for a, _k in sent]
    assert "tasks.rolling_period_compose" in names
    for args, kwargs in sent:
        assert kwargs.get("kwargs", {}).get("tenant_id"), "tenant_id must travel as a kwarg"
        assert len(args) == 1, "task name only — no positional tenant_id"
    assert str(tenant.id) in [k["kwargs"]["tenant_id"] for _a, k in sent]


# --- T2 gate round 1: the cost ceiling must bound ATTEMPTS, not successes -----------


async def test_budget_cap_bounds_attempts_not_just_successes(db, monkeypatch, tenant_user):
    """T2 gate round 1 (major): the cap gated on stats["composed"], which only increments
    on SUCCESS. During a NetSuite outage — precisely the condition the ceiling exists to
    protect against — every compose fails, "composed" stays 0, the guard never trips, and
    the sweep attempts EVERY behind series at 4-6 NetSuite round trips each against a
    120/min limit shared with live chat. The module docstring claimed this satisfied
    agent-graph.md #6 ("Bound cost, not just call count") while bounding neither in the
    failure case. Cost is incurred by the ATTEMPT, so the attempt is what must be capped.
    """
    tenant, user = tenant_user
    for key in ("income_statement", "balance_sheet", "trial_balance"):
        await _series(db, tenant, user, key, newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def always_fails(_key):
        raise RefreshError(502, "NetSuite is down")

    calls = _patch_compose(monkeypatch, always_fails)
    result = await sweep_tenant_series(db, tenant.id, max_composes=2)

    assert len(calls) == 2, "a failing compose still burns NetSuite calls — cap the attempts"
    assert result["failed"] == 2
    assert result["remaining"] == 1
    assert result["reason"] == REASON_BUDGET


async def test_a_failed_series_rolls_back_so_the_next_one_is_not_poisoned(db, monkeypatch, tenant_user):
    """T2 gate round 1 (major): a DB-level failure inside compose_playbook_report (it has
    no internal try/except, and it commits) leaves the shared AsyncSession in
    pending-rollback. Without a rollback here, the NEXT series fails immediately with
    PendingRollbackError on its first query — for a completely different reason than its
    own — which defeats the per-series isolation this loop documents AND poisons the stall
    fingerprint with a fabricated failure mode. report_auto_refresh.py:177 rolls back in
    exactly this position for exactly this reason."""
    tenant, user = tenant_user
    for key in ("income_statement", "balance_sheet"):
        await _series(db, tenant, user, key, newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    rollbacks = {"n": 0}
    real_rollback = db.rollback

    async def counting_rollback():
        rollbacks["n"] += 1
        await real_rollback()

    monkeypatch.setattr(db, "rollback", counting_rollback)

    async def always_fails(_key):
        raise RefreshError(502, "db blew up mid-compose")

    _patch_compose(monkeypatch, always_fails)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["failed"] == 2
    assert rollbacks["n"] == 2, "every failed series must leave the session usable for the next"


# --- T2 gate round 2 --------------------------------------------------------------


async def test_a_single_failure_is_an_error_not_a_stall(db, monkeypatch, tenant_user):
    """A stall means "identical failures ACROSS ATTEMPTS, so re-running will not help".
    One attempt is not a pattern. Calling a lone transient 502 a stall tells the ops
    digest a mechanism change is needed when a retry tomorrow would have fixed it — and
    the whole point of the reason enum is that a human can route on it."""
    tenant, user = tenant_user
    await _series(db, tenant, user, "income_statement", newest_period="May 2026")
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def one_transient(_key):
        raise RefreshError(502, "transient blip")

    _patch_compose(monkeypatch, one_transient)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["failed"] == 1
    assert result["reason"] == REASON_ERROR, "one failure is not a stall"


async def test_a_tenant_with_no_series_never_pays_for_period_resolution(db, monkeypatch, tenant_user):
    """Resolving costs 2 NetSuite round trips and a tenant with no series has nothing to
    compose whatever it returns. Most tenants track nothing, so resolving first would put
    the sweep's dominant cost on the tenants that need it least."""
    tenant, _user = tenant_user
    resolved = {"n": 0}

    async def counting_resolve(db_, tenant_id_):
        resolved["n"] += 1
        return _JUN_CLOSED

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period_cached", counting_resolve)
    result = await sweep_tenant_series(db, tenant.id)

    assert result["reason"] == "done"
    assert result["series_total"] == 0
    assert resolved["n"] == 0, "must not resolve for a tenant with nothing to compose"


def test_auto_compose_is_scheduled_is_false_when_the_cap_is_zero(monkeypatch):
    """Gate round 2: the ribbon gated only on the ENABLED flag, so a cap of 0 — a
    plausible way to throttle during a NetSuite rate-limit incident — stopped every
    compose while the UI kept promising one. Both the sweep and the ribbon now ask this
    ONE predicate; a raw flag read in two modules is how the hole opened."""
    from app.core.config import settings as app_settings
    from app.workers.tasks.rolling_period_compose import auto_compose_is_scheduled

    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED", True)
    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_COMPOSE_MAX_PER_TENANT", 0)
    assert auto_compose_is_scheduled() is False

    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_COMPOSE_MAX_PER_TENANT", 10)
    assert auto_compose_is_scheduled() is True

    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED", False)
    assert auto_compose_is_scheduled() is False
