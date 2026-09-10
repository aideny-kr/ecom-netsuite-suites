"""Step registry — the allow-list for Scheduled Jobs plans.

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B2 (binding): ``STEP_REGISTRY`` is the ONLY place a step type is defined. The
compiler's structured-output schema (Task 2) is generated from it via
``plan_schema()``, and a compiled plan is validated against it twice — once here
at compile time (``validate_plan``), and again at run time by the executor
(Task 4) simply looking the step's type up in ``STEP_REGISTRY`` before running
it. Two choke points, one registry: nothing outside this module can grow the set
of things a job is allowed to do.

v1 has six step types (§B2's own list, verbatim): ``bigquery_sql`` (read),
``report.compose`` (read — compose a NEW report from a playbook, or refresh an
EXISTING one by id), ``report.render_pdf`` (read), ``report.build_xlsx`` (read),
``drive.upload`` (write — the only write step in v1), ``recon.run`` (read — the
existing order-level reconciliation engine on a trailing window; a recon step
never approves, locks, or posts anything — that stays a human's job on the recon
run page). Every executor below is wired to a REAL function that already ships
on this branch (Slice 1's report pipeline, the existing reconciliation engine,
the existing BigQuery tool) — nothing here is a stub; see each executor's
docstring for the exact function it calls and why.

Cross-step artifact references
-------------------------------
A step's params can point at an EARLIER step's output by id (e.g. ``drive.upload``
needs to know which ``report.compose`` step produced the report it delivers).
Such a param is marked in its ``params_schema`` with the vendor JSON Schema
keyword ``"x-step-ref": true`` (a real validator ignores unknown keywords, so
this stays valid JSON Schema while ``validate_plan`` can walk the schema and
find these params generically, without a step type needing any bespoke
cross-reference code of its own). ``validate_plan`` rejects a reference to a
step id that is not defined EARLIER in the same plan — a step's inputs must
already exist by the time it runs; a forward or missing reference is invalid
regardless of which step type does the referencing.

Executor calling convention
----------------------------
Every executor is an ``async def executor(ctx: StepContext, params: dict) -> dict``.
It returns an ARTIFACT dict that the run loop (Task 4) stores at
``ctx.artifacts[<this step's id>]`` before running the next step — that dict is
in-memory only for the lifetime of one run (it may hold live objects, e.g. a
``Report`` ORM row, not just JSON-safe values; only a distilled, JSON-safe
subset belongs in the persisted ``jobs.result_summary``, and building that
summary is the run loop's job, not the registry's).

Budget enforcement ("budget enforced between steps", §B4) and the WRITE-step
"audit `started` before the call" convention are also the run loop's
responsibility, not the registry's: ``StepContext.budget`` is a plain dict the
loop threads through and checks between calls, and ``StepSpec.idempotency`` is
handed to the loop as the KEY it audits before invoking a write executor — the
registry does not write audit events itself so that every step type gets that
guarantee from ONE place (the loop) rather than reimplementing it per step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import jsonschema
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.services.report.report_delivery import DeliveryIdentity

StepExecutor = Callable[["StepContext", dict], Awaitable[dict]]
IdempotencyFn = Callable[["StepContext", dict], str]

_VALID_KINDS = ("read", "write")


class PlanInvalid(ValueError):  # noqa: N818 — interface name from spec §B2/§B3, not a generic Error
    """Raised by ``validate_plan`` with one message per invalid step (never just
    the first) — the compiler's repair round needs to see everything wrong with
    a plan in one shot, not fix one problem and resubmit N times."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors) or "plan is invalid")


class StepExecutionError(RuntimeError):
    """Raised by an executor at RUN time (never at compile time — that's
    ``PlanInvalid``) when a step cannot do its job: a referenced artifact is
    missing from ``ctx.artifacts``, or the wrapped call itself failed. The run
    loop (Task 4) catches this to produce ``result_summary.reason == "error"``."""


@dataclass
class StepContext:
    """Everything an executor needs, threaded through one run by the run loop.

    ``job_id`` is the SCHEDULE's own stable id — the same value on every run and
    every retry of the same period, which is exactly what a WRITE step's
    idempotency key needs (spec §0.5 / the mock: "idempotency key = job +
    snapshot date"; a retry of a failed run must reuse the SAME key so the
    retried upload replaces rather than duplicates). ``run_id`` is this
    particular attempt's ``jobs`` row id, for correlation/audit only — using it
    in an idempotency key would defeat the point, since a retry gets a NEW
    ``jobs`` row and therefore a new run_id.

    ``artifacts`` accumulates one entry per step, keyed by that step's plan id,
    written by the run loop after each successful executor call — a step's
    params reference an earlier entry by that same id (see the module
    docstring's "Cross-step artifact references").

    ``current_step_id`` (item 1, delta gate fix #2): the run loop
    (``_run_steps``, ``app.workers.tasks.scheduled_jobs``) sets this to the
    plan id of whichever step is ABOUT to run, before calling its executor —
    ``_report_compose_executor`` reads it to stamp
    ``schedule_delivery_identity(ctx.job_id, ctx.current_step_id)`` onto the
    composed/refreshed report's own ``delivery_json["identity"]`` right after
    compose returns, so a LATER step's failure (e.g. ``drive.upload``) can
    never lose the identity a re-delivery will need to recover. ``None`` only
    for a ``StepContext`` built by a test that never goes through the real
    run loop.
    """

    job_id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    db: AsyncSession
    budget: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    period_key: str | None = None
    actor_type: str = "system"
    actor_id: uuid.UUID | None = None
    current_step_id: str | None = None


@dataclass(frozen=True)
class StepSpec:
    type: str
    label: str
    kind: str  # "read" | "write"
    params_schema: dict
    executor: StepExecutor
    idempotency: IdempotencyFn | None = None

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"StepSpec.kind must be one of {_VALID_KINDS!r}, got {self.kind!r}")
        if self.kind == "write" and self.idempotency is None:
            raise ValueError(f"StepSpec {self.type!r} is a write step and must declare an idempotency function")
        if self.kind == "read" and self.idempotency is not None:
            raise ValueError(f"StepSpec {self.type!r} is a read step and must not declare an idempotency function")


@dataclass(frozen=True)
class PlanStep:
    id: str
    type: str
    params: dict


@dataclass(frozen=True)
class ValidatedPlan:
    steps: list[PlanStep]
    raw: dict


# ---------------------------------------------------------------------------
# Executors — each wraps a REAL function already shipping on this branch.
# ---------------------------------------------------------------------------


async def _bigquery_sql_executor(ctx: StepContext, params: dict) -> dict:
    """Wraps ``app.mcp.tools.bigquery_tools.bigquery_sql_execute`` — the same
    tool the chat agent calls, given the ``{tenant_id, db}`` context shape it
    already expects."""
    from app.mcp.tools.bigquery_tools import bigquery_sql_execute

    result = await bigquery_sql_execute(
        {"query": params["query"], "max_rows": params.get("max_rows", 1000)},
        context={"tenant_id": ctx.tenant_id, "db": ctx.db},
    )
    if result.get("error"):
        raise StepExecutionError(f"bigquery_sql: {result.get('message')}")
    return result


async def _report_compose_executor(ctx: StepContext, params: dict) -> dict:
    """Either composes a NEW report from a playbook (``compose_playbook_report``)
    or refreshes an EXISTING one (``refresh_report``) — ``validate_plan`` already
    guarantees exactly one of ``playbook_key``+``params`` or ``report_id`` is
    present (its ``oneOf`` schema). Returns an artifact carrying both the plain
    fields later steps need (``report_id``, ``rendered_html``, ``period_key``)
    AND the live ``Report`` row itself, so ``report.build_xlsx`` can reuse
    ``report_delivery._inventory_aging_model`` exactly as ``deliver_report_to_drive``
    does internally, instead of re-deriving that routing logic here.

    Item 1 (delta gate fix #2): right after compose/refresh returns, this
    stamps ``schedule_delivery_identity(ctx.job_id, ctx.current_step_id)``
    onto ``report.delivery_json["identity"]`` and flushes (never commits —
    the run loop owns commits). A later ``drive.upload`` step references this
    SAME compose step by id (``params["report_step"]``), so the identity
    stamped here is byte-identical to the one ``_drive_upload_executor``
    would build for it — a failure in that (or any later) step can never
    leave the report without a recoverable identity, because the run loop
    commits BEFORE the next step's executor runs (module docstring's
    "Idempotency + audit-before-call"; for the common
    ``compose -> ... -> drive.upload`` shape, that next step's own
    audit-before-call commit is what makes THIS flush durable, before
    ``drive.upload``'s own executor ever gets a chance to fail)."""
    if "report_id" in params:
        from app.services.report.refresh_service import refresh_report

        report = await refresh_report(
            ctx.db,
            report_id=uuid.UUID(params["report_id"]),
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            actor_type=ctx.actor_type,
        )
    else:
        from app.services.report.playbooks import compose_playbook_report

        report = await compose_playbook_report(
            ctx.db,
            playbook_key=params["playbook_key"],
            params=params["params"],
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            actor_type=ctx.actor_type,
            mode=params.get("mode", "period"),
        )

    # compose/refresh may have committed mid-flight (an OAuth token refresh,
    # refresh_report's own claim commit, ...) which clears the transaction
    # -scoped tenant GUC — re-assert before this write, a safe no-op when it
    # is already set (agent-graph.md / this branch's own repeated trap).
    from app.core.database import set_tenant_context

    await set_tenant_context(ctx.db, str(ctx.tenant_id))
    identity = schedule_delivery_identity(ctx.job_id, ctx.current_step_id)
    report.delivery_json = {
        **(report.delivery_json or {}),
        "identity": {
            "folder_props": identity.folder_props,
            "file_props": identity.file_props,
            "lock_key": identity.lock_key,
            "idempotency_prefix": identity.idempotency_prefix,
        },
    }
    await ctx.db.flush()

    return {
        "report": report,
        "report_id": str(report.id),
        "rendered_html": report.rendered_html,
        "title": report.title,
        "version": report.version,
    }


def schedule_delivery_identity(schedule_id: uuid.UUID, report_step: str) -> "DeliveryIdentity":
    """The ONE construction of a schedule-keyed ``DeliveryIdentity`` (item 1,
    delta gate fix #2) — used by both ``_drive_upload_executor`` (passed
    explicitly to ``deliver_report_to_drive``) and ``_report_compose_executor``
    (stamped onto ``report.delivery_json["identity"]`` right after compose, so
    a later manual re-delivery recovers the SAME identity a scheduled upload
    would have used). ``folder_props`` stays schedule-only (one Drive folder
    per schedule); ``file_props``/``lock_key``/``idempotency_prefix`` all also
    carry the PRODUCING ``report.compose`` step's id (item 3, delta gate fix:
    a plan with two ``report.compose -> drive.upload`` chains must not
    collide on the same file identity or advisory lock). ``file_props`` never
    carries ``period_key`` — ``deliver_report_to_drive`` merges the CURRENT
    call's period into both ``file_props`` and the idempotency key itself, so
    a period baked in here could go stale across a later delivery of a
    different period (item 1's own fix)."""
    from app.services.report.report_delivery import DeliveryIdentity

    sid = str(schedule_id)
    return DeliveryIdentity(
        folder_props={"schedule_id": sid},
        file_props={"schedule_id": sid, "report_step": report_step},
        lock_key=f"schedule:{sid}:{report_step}",
        idempotency_prefix=f"job-delivery:{sid}:{report_step}",
    )


def _resolve_report_step_artifact(ctx: StepContext, params: dict) -> dict:
    """Shared lookup for every step type that references a ``report.compose``
    step by id (``report.render_pdf``, ``report.build_xlsx``, ``drive.upload``).
    ``validate_plan`` already proved the id exists somewhere earlier in the
    plan; this raises ``StepExecutionError`` (a RUN-time problem, not a compile
    one) only for the case validate_plan cannot see — the referenced step ran
    but produced no usable artifact (defensive; should not happen for a
    well-formed registry)."""
    step_id = params["report_step"]
    artifact = ctx.artifacts.get(step_id)
    if not artifact or "report" not in artifact:
        raise StepExecutionError(f"report_step {step_id!r} produced no report artifact")
    return artifact


async def _report_render_pdf_executor(ctx: StepContext, params: dict) -> dict:
    """Wraps Task 4 (Slice 1)'s ``render_report_pdf`` over the referenced
    ``report.compose`` step's ``rendered_html``. WeasyPrint is imported lazily
    INSIDE ``render_report_pdf`` itself (see that module's docstring), so this
    module stays importable everywhere regardless of native libs."""
    from app.services.report.report_pdf import render_report_pdf

    artifact = _resolve_report_step_artifact(ctx, params)
    pdf_bytes = render_report_pdf(artifact["rendered_html"], appendix_html=params.get("appendix_html"))
    return {"pdf_bytes": pdf_bytes, "report_id": artifact["report_id"]}


async def _report_build_xlsx_executor(ctx: StepContext, params: dict) -> dict:
    """Mirrors ``report_delivery._render_xlsx_bytes`` exactly (reused, not
    duplicated): an inventory_aging report gets the real seven-sheet workbook
    via ``report_excel.build_inventory_aging_workbook``; every other report
    type falls back to a generic single-sheet metadata workbook via the shared
    ``build_workbook`` — the same fallback ``deliver_report_to_drive`` uses so a
    delivery always has SOME Excel artifact."""
    from app.services.report.report_delivery import _inventory_aging_model

    artifact = _resolve_report_step_artifact(ctx, params)
    report = artifact["report"]
    ia_model = _inventory_aging_model(report)
    if ia_model is not None:
        from app.services.report.report_excel import build_inventory_aging_workbook

        xlsx_bytes = build_inventory_aging_workbook(ia_model).getvalue()
    else:
        from app.services.reconciliation.evidence_service import SheetSpec, build_workbook

        sheet: SheetSpec = {
            "name": "Report",
            "headers": ["Field", "Value"],
            "rows": [
                ["Title", report.title],
                ["Status", report.status],
                ["Version", report.version],
            ],
        }
        xlsx_bytes = build_workbook([sheet]).getvalue()
    return {"xlsx_bytes": xlsx_bytes, "report_id": artifact["report_id"]}


async def _drive_upload_executor(ctx: StepContext, params: dict) -> dict:
    """Delegates the entire delivery to ``deliver_report_to_drive`` — the one
    function that already owns folder/file idempotency (found-or-created by
    Drive ``appProperties``), the started/completed audit pair, and a
    per-delivery advisory lock against a concurrent delivery. It re-derives
    PDF/Excel bytes from the report itself rather than accepting the bytes
    ``report.render_pdf``/``report.build_xlsx`` already produced — a
    deliberate seam, not an oversight: reimplementing Drive's
    find-or-update-by-identity + locking here to save one re-render would
    duplicate exactly the machinery this executor exists to reuse correctly.
    The earlier render/xlsx steps still run and their artifacts are attached
    to the job's own run record for provenance, matching the binding mock's
    step 3 guard line ("local artifacts, attached to the run").

    The period is the RUN's (``ctx.period_key`` — the due date in the
    schedule's timezone, set by the run loop), never a value from ``params``:
    a plan is compiled once and replayed every week, so a literal baked into
    the plan would be the compile date on every run — the Drive filename
    (``<title> — <period_key>.pdf``) and the idempotency key below would never
    vary between weeks. ``_DRIVE_UPLOAD_SCHEMA`` rejects a compiled
    ``period_key`` outright so the compiler cannot produce that plan.

    Drive identity is the SCHEDULE, never the report row (item 9, gate fix):
    ``_report_compose_executor`` composes a NEW ``Report`` every run
    (inventory_aging is ``period_based: False``, so tracking/series mode is
    refused and ``mode="period"`` is what runs) — the default report/series
    -keyed identity ``deliver_report_to_drive`` falls back to when
    ``identity=None`` would create a NEW Drive folder every Monday. Passing
    an explicit ``DeliveryIdentity`` keyed on ``ctx.job_id`` (the SCHEDULE's
    own stable id — see ``StepContext``'s docstring) fixes that: every run of
    the same schedule resolves to the SAME folder, and a re-delivery of the
    same period replaces the SAME files rather than duplicating them.

    Item 3 (delta gate fix): the FOLDER stays keyed on ``schedule_id`` alone
    (one Drive folder per schedule is correct), but ``file_props``/
    ``lock_key``/``idempotency_prefix`` also carry the PRODUCING
    ``report.compose`` step's id (``params["report_step"]``) — a plan with
    TWO ``report.compose -> drive.upload`` chains (two different reports
    delivered by the same schedule run) previously collided on the exact
    same file identity AND the exact same advisory lock, so the second
    upload's find-then-update silently overwrote the first's files instead
    of each keeping its own. Item 1 (delta gate fix #2): the identity is now
    built via the shared ``schedule_delivery_identity`` helper — the SAME one
    ``_report_compose_executor`` uses to stamp
    ``report.delivery_json["identity"]`` right after compose, so a later
    manual re-delivery recovers byte-identical props to what a scheduled
    upload would have built here.

    Accepted wart: a retry after a partial upload (the pdf lands, the xlsx
    fails, the run retries) composes a SECOND ``Report`` row for that Monday
    — ``_report_compose_executor`` has no way to know a previous attempt's
    row exists, since the executor is stateless between run attempts. Drive
    itself stays clean (the second attempt's upload finds and replaces the
    SAME files by schedule identity); only the ``reports`` table accumulates
    an extra row for that period. Deduplicating ``reports`` rows across a
    retry is a separate, deliberate non-goal of this fix."""
    from app.services.report.report_delivery import deliver_report_to_drive

    artifact = _resolve_report_step_artifact(ctx, params)
    period_key = ctx.period_key
    if not period_key:
        raise StepExecutionError("drive.upload: the run supplied no period_key")

    identity = schedule_delivery_identity(ctx.job_id, params["report_step"])

    result = await deliver_report_to_drive(
        ctx.db,
        tenant_id=ctx.tenant_id,
        report_id=uuid.UUID(artifact["report_id"]),
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        period_key=period_key,
        identity=identity,
    )
    return {
        "pdf_file_id": result.pdf_file_id,
        "pdf_url": result.pdf_url,
        "xlsx_file_id": result.xlsx_file_id,
        "xlsx_url": result.xlsx_url,
        "folder_id": result.folder_id,
        "period_key": period_key,
        "delivered_at": result.delivered_at.isoformat() if result.delivered_at else None,
    }


def _drive_upload_idempotency(ctx: StepContext, params: dict) -> str:
    """Spec §0.5 / the mock's own copy: "idempotency key = job + snapshot date".
    ``job`` is the SCHEDULE's stable id (``ctx.job_id``, NOT ``ctx.run_id`` — see
    ``StepContext``'s docstring for why a retry must reuse this key); the
    snapshot date is the RUN's ``period_key`` (see ``_drive_upload_executor``),
    so a retry of the same period reuses the key and next week's run gets a
    new one. Deliberately never raises (the run loop calls it outside its
    per-step try): a missing run period surfaces as the executor's own
    ``StepExecutionError`` on the very next line of the loop."""
    return f"job:{ctx.job_id}:period:{ctx.period_key}"


async def _recon_run_executor(ctx: StepContext, params: dict) -> dict:
    """Wraps the SAME async function the ``tasks.reconciliation_run`` Celery task
    itself calls in-process (``_execute``) — always ``match_level="order"``, the
    product-default OrderReconJob engine ``recon_scheduled_run_all.py`` already
    uses for its own nightly sweep. Read + match only: OrderReconJob never
    approves, locks, or posts — needs-review lines wait on the recon run page
    for a person, unchanged by running from a scheduled job.

    The window's end date is the run's own ``period_key`` (spec §B4) —
    ``run_schedule_now`` already computes this as ``due_at`` converted into
    the SCHEDULE's own timezone (``app.workers.tasks.scheduled_jobs``), not a
    naive ``date.today()`` (review finding): near midnight UTC, a schedule in
    a non-UTC timezone (e.g. ``America/Los_Angeles``) and the server's UTC
    wall clock disagree on what "today" is, and the window must follow the
    schedule's own date, not the server's. ``ctx.period_key`` is only unset
    for a ``StepContext`` built without a real run (no production caller does
    this) — fall back to ``date.today()`` defensively rather than raise."""
    from datetime import date, timedelta

    from app.workers.tasks.reconciliation_run import _execute

    window_days = params.get("window_days", 7)
    today = date.fromisoformat(ctx.period_key) if ctx.period_key else date.today()
    summary = await _execute(
        ctx.db,
        tenant_id=str(ctx.tenant_id),
        date_from=(today - timedelta(days=window_days)).isoformat(),
        date_to=today.isoformat(),
        subsidiary_id=params.get("subsidiary_id"),
        payout_ids=None,
        job_id=str(ctx.run_id),
        match_level="order",
    )
    return {"recon_summary": summary}


# ---------------------------------------------------------------------------
# Params schemas — JSON Schema, also emitted (via plan_schema()) to the
# compiler as its structured-output contract. "x-step-ref": true marks a
# param that must name an earlier step's id (see module docstring).
# ---------------------------------------------------------------------------

_BIGQUERY_SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "description": "The final BigQuery SQL to run, read-only."},
        "max_rows": {"type": "integer", "minimum": 1, "maximum": 100000},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_REPORT_COMPOSE_SCHEMA = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "playbook_key": {"type": "string", "minLength": 1},
                "params": {"type": "object"},
                "mode": {"type": "string", "enum": ["period", "tracking"]},
            },
            "required": ["playbook_key", "params"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "report_id": {"type": "string", "minLength": 1},
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
    ],
}

_REPORT_RENDER_PDF_SCHEMA = {
    "type": "object",
    "properties": {
        "report_step": {"type": "string", "minLength": 1, "x-step-ref": True},
        "appendix_html": {"type": "string"},
    },
    "required": ["report_step"],
    "additionalProperties": False,
}

_REPORT_BUILD_XLSX_SCHEMA = {
    "type": "object",
    "properties": {
        "report_step": {"type": "string", "minLength": 1, "x-step-ref": True},
    },
    "required": ["report_step"],
    "additionalProperties": False,
}

# No `period_key` param on purpose: the period is the run's own (see
# `_drive_upload_executor`). `additionalProperties: False` is what rejects a
# compiled literal — at compile time (validate_plan) and, via plan_schema(),
# in the structured-output contract the compiler's LLM call is bound to.
_DRIVE_UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "report_step": {"type": "string", "minLength": 1, "x-step-ref": True},
    },
    "required": ["report_step"],
    "additionalProperties": False,
}

_RECON_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "window_days": {"type": "integer", "minimum": 1, "maximum": 90},
        "subsidiary_id": {"type": "string"},
    },
    "additionalProperties": False,
}


STEP_REGISTRY: dict[str, StepSpec] = {
    "bigquery_sql": StepSpec(
        type="bigquery_sql",
        label="BigQuery SQL query",
        kind="read",
        params_schema=_BIGQUERY_SQL_SCHEMA,
        executor=_bigquery_sql_executor,
    ),
    "report.compose": StepSpec(
        type="report.compose",
        label="Compose report",
        kind="read",
        params_schema=_REPORT_COMPOSE_SCHEMA,
        executor=_report_compose_executor,
    ),
    "report.render_pdf": StepSpec(
        type="report.render_pdf",
        label="Render PDF",
        kind="read",
        params_schema=_REPORT_RENDER_PDF_SCHEMA,
        executor=_report_render_pdf_executor,
    ),
    "report.build_xlsx": StepSpec(
        type="report.build_xlsx",
        label="Build Excel workbook",
        kind="read",
        params_schema=_REPORT_BUILD_XLSX_SCHEMA,
        executor=_report_build_xlsx_executor,
    ),
    "drive.upload": StepSpec(
        type="drive.upload",
        label="Upload to Google Drive",
        kind="write",
        params_schema=_DRIVE_UPLOAD_SCHEMA,
        executor=_drive_upload_executor,
        idempotency=_drive_upload_idempotency,
    ),
    "recon.run": StepSpec(
        type="recon.run",
        label="Run reconciliation",
        kind="read",
        params_schema=_RECON_RUN_SCHEMA,
        executor=_recon_run_executor,
    ),
}


def plan_schema() -> dict:
    """The compiler's (Task 2) structured-output contract: an object with one
    ``steps`` array, each item a ``{id, type, params}`` triple where ``type`` is
    constrained to exactly the registry's keys — generated fresh from
    ``STEP_REGISTRY`` every call, so the registry stays the single source of
    truth (a hand-maintained copy of the type enum would drift the moment a
    step type is added or removed here)."""
    return {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "type": {"type": "string", "enum": sorted(STEP_REGISTRY)},
                        "params": {"type": "object"},
                    },
                    "required": ["id", "type", "params"],
                },
            },
        },
        "required": ["steps"],
        "$defs": {step_type: spec.params_schema for step_type, spec in STEP_REGISTRY.items()},
    }


def _step_ref_keys(params_schema: dict) -> list[str]:
    """Every property key marked ``"x-step-ref": true`` in ``params_schema`` —
    walks both a plain object schema and each branch of a ``oneOf``, since a
    step-ref param can live inside either shape."""
    keys: list[str] = []
    branches = [params_schema, *(params_schema.get("oneOf") or [])]
    for branch in branches:
        for key, sub in (branch.get("properties") or {}).items():
            if isinstance(sub, dict) and sub.get("x-step-ref") and key not in keys:
                keys.append(key)
    return keys


def validate_plan(plan: dict) -> ValidatedPlan:
    """Validate a compiled plan against the registry (spec §B3): every step's
    ``type`` must be a registry key, its ``params`` must satisfy that type's
    JSON Schema, and every step-reference param must name a step id defined
    STRICTLY EARLIER in the same plan. Collects one message per bad step (never
    just the first) and raises ``PlanInvalid`` with all of them, or returns a
    ``ValidatedPlan`` when the whole plan is clean."""
    if not isinstance(plan, dict):
        raise PlanInvalid(["plan must be a JSON object"])

    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanInvalid(["plan.steps must be a non-empty array"])

    errors: list[str] = []
    steps: list[PlanStep] = []
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_steps):
        prefix = f"step {idx + 1}"
        if not isinstance(raw, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        step_id = raw.get("id")
        step_type = raw.get("type")
        params = raw.get("params")

        if not isinstance(step_id, str) or not step_id:
            errors.append(f"{prefix}: missing a step id")
            continue
        if step_id in seen_ids:
            errors.append(f"{prefix} ({step_id}): duplicate step id")
            continue
        seen_ids.add(step_id)

        spec = STEP_REGISTRY.get(step_type)
        if spec is None:
            errors.append(f"{prefix} ({step_id}): unknown step type {step_type!r} — not in the registry")
            continue

        if not isinstance(params, dict):
            errors.append(f"{prefix} ({step_id}): params must be an object")
            continue

        try:
            jsonschema.validate(instance=params, schema=spec.params_schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{prefix} ({step_id}): {exc.message}")
            continue

        step_errors = []
        for ref_key in _step_ref_keys(spec.params_schema):
            ref_value = params.get(ref_key)
            if ref_value is None:
                continue
            ref_targets = ref_value if isinstance(ref_value, list) else [ref_value]
            for target in ref_targets:
                # Steps SEEN so far are only the ones already appended to `steps`
                # (this step's own id was added to seen_ids above for the
                # duplicate check, but must not count as "produced earlier").
                if target not in {s.id for s in steps}:
                    step_errors.append(
                        f"{prefix} ({step_id}): {ref_key!r} references step {target!r}, which no earlier step produces"
                    )
        if step_errors:
            errors.extend(step_errors)
            continue

        steps.append(PlanStep(id=step_id, type=step_type, params=params))

    if errors:
        raise PlanInvalid(errors)

    return ValidatedPlan(steps=steps, raw=plan)
