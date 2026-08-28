"""Task 6: error snapshotting + signature grouping.

**This is the task the whole Plan B flow-map plan exists for.** Open Celigo
errors observed 2026-08-05 and 2026-08-07 are within Celigo's ~30-day purge
window (observed-shapes.md, live-probed 2026-08-27) and get permanently
destroyed around 2026-09-09 unless snapshotted first.

Two public functions:

  * `normalize_message(message)` / `fingerprint(source, code, message)` --
    a PII-safe grouping key. `message` text carries customer emails and
    order refs VERBATIM by design (sanitizer.py's own docstring: "the
    message IS the diagnosis", deliberately never stripped there). PII
    safety for GROUPING happens here, in the fingerprint, by normalizing a
    disposable copy of the message before hashing -- never by mutating or
    logging the stored `message`/`sample_message` columns themselves, which
    stay verbatim and PII-bearing on purpose.
  * `upsert_errors(db, *, tenant_id, connection_id, step, raw_errors)` --
    given one flow step's CURRENT list of open errors from Celigo (the
    shape `client.list_flow_errors_for_step` returns), upserts one
    `celigo_error_signatures` row per distinct fingerprint and one
    `celigo_flow_errors` row per error, then resolves (never deletes) any
    error this step previously had open that is absent from *raw_errors*.

NORMALIZATION IS DELIBERATELY NARROW (team lead ruling, 2026-08-27): digits,
emails, `R\\d+`-style order refs, UUIDs, and ISO-8601 timestamps fold to
placeholder tokens before hashing -- nothing else. Over-normalising merges
genuinely different failures, which is worse than under-merging: an
under-merged signature is a visible duplicate an engineer can spot; an
over-merged one silently hides a distinct root cause. Do not add another
pattern here without a live-observed case that needs it.

STEP 5's "~3 signatures" FIGURE IS STALE, DO NOT TUNE TO IT: the brief's
acceptance step ties "30 errors across 4 flows" to "roughly 3 signatures",
but observed-shapes.md (probed live 2026-08-27) found the real account has
103 open errors across 13+ scanned flows (only 100 of 208 flows were even
scanned), long-tailed rather than evenly spread -- two flows alone carry 32
and 24. This module's tests assert the normalizer's PROPERTIES instead of
any absolute count: same-shape variants collapse to one signature, a
structurally different error does not, and no PII survives into the
`fingerprint` column. See task-6-report.md for the number actually observed
against this module's live-shaped (but synthetic-valued) fixtures.

DEFENSIVE RE-SANITIZATION: *raw_errors* items are run through
`sanitize("error", raw)` again in this module, even though today's only
caller (`client.list_flow_errors_for_step`) already sanitizes before
returning. `sanitize()` is a pure allowlist filter over a flat schema, so
re-applying it is a no-op on an already-sanitized dict -- it costs nothing
and matches the sanitizer's own stated posture ("every response must pass
through sanitize() before it is stored, logged, or returned -- including
error paths"). This module is the last stop before an error reaches the
database; it does not trust that some upstream caller remembered.

RESOLUTION SCOPE, DELIBERATELY NARROW: `upsert_errors` resolves an error
that disappeared from *this step's* current listing. It does NOT mark
anything `purged` -- that requires comparing each error's own `purge_at`
against wall-clock `now()` independently of any single step's sync
(`repository.mark_flow_errors_purged`'s own docstring: "the caller decides
this, typically by comparing against each error's own purge_at"), which is
a scheduling concern for whatever orchestrates repeated calls into this
module, not something one per-step snapshot can decide alone. It also does
NOT un-resolve an error that reappears under the same `celigo_id` after
being marked resolved -- there is no live evidence a resolved Celigo error
ever reopens under its original id, and inventing that behavior without
evidence is exactly the failure class this session's other falsified
assumptions warn against.

OCCURRENCE_COUNT IS RECOMPUTED FROM STORED ROWS, NEVER INCREMENTED IN
PLACE: `repository.upsert_error_signature`'s own docstring names "an
error-normalizer" (this module) as the owner of `occurrence_count`/
`first_seen`/`last_seen` aggregation semantics. A naive `+= len(raw_errors)`
on every call would double-count every already-synced error on every
re-sync -- the same open `celigo_id` is fetched from Celigo repeatedly
until it resolves, sometimes over many days. Instead, after upserting this
call's `celigo_flow_errors` rows, `occurrence_count` is recomputed as
`COUNT(*) FILTER (WHERE resolved_at IS NULL)` and `first_seen`/`last_seen`
as `MIN`/`MAX(occurred_at)`, all scoped to `signature_id` (not to this
call's step or even this call's flow) -- idempotent by construction
(re-upserting the same `celigo_id` updates one row, never adds one), and
it correctly reflects contributions from every OTHER step that happens to
share the same fingerprint, not just the step this call is processing.
`first_seen`/`last_seen` intentionally span resolved AND open rows (the
full historical range); `occurrence_count` intentionally counts only
currently-open rows (a resolved failure isn't still "occurring").

ORDERING WITHIN ONE CALL (FIX ROUND 1, team lead, 2026-08-27 -- caught by an
executed repro, not a reading): this call's own `mark_flow_errors_resolved`
MUST run BEFORE the phase-3 recompute above, never after. The recompute
filters `WHERE resolved_at IS NULL`; if resolution ran second, the count
would be stale by exactly the set this call just resolved. That staleness
is not self-healing -- if the underlying root cause is genuinely fixed and
never appears in any future `raw_errors` batch, nothing ever touches that
`signature_id` again, so a wrong-ordered call would leave `occurrence_count`
frozen at its last-open value forever, permanently overstating an
already-resolved root cause on any dashboard reading that column.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlowError, CeligoFlowStep
from app.services.celigo.repository import (
    mark_flow_errors_resolved,
    upsert_error_signature,
    upsert_flow_error,
)
from app.services.celigo.sanitizer import sanitize

# ---------------------------------------------------------------------------
# normalize_message() / fingerprint() -- pure, no DB, no PII in the output.
# ---------------------------------------------------------------------------

# Order matters: each pattern below replaces a whole match with one token
# BEFORE the next, narrower pattern runs -- an email or ISO timestamp must
# be swallowed whole before the generic digit catch-all would otherwise
# mangle the digits inside it. See module docstring: this list is
# deliberately narrow and closed; extend only with live-observed evidence.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ISO_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:?\d{2})?\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ORDER_REF_RE = re.compile(r"\bR\d+\b")
_DIGITS_RE = re.compile(r"\d+")


def normalize_message(message: str | None) -> str:
    """Return a PII-safe, grouping-oriented copy of *message* -- never
    stored, never logged; only ever fed into `fingerprint`'s hash. Folds
    emails, ISO-8601 timestamps, UUIDs, `R\\d+`-style order refs, and any
    remaining digit runs to fixed placeholder tokens, in that order (see the
    comment above the regexes for why order matters)."""
    if not message:
        return ""
    normalized = _EMAIL_RE.sub("<EMAIL>", message)
    normalized = _ISO_TIMESTAMP_RE.sub("<TS>", normalized)
    normalized = _UUID_RE.sub("<UUID>", normalized)
    normalized = _ORDER_REF_RE.sub("<REF>", normalized)
    normalized = _DIGITS_RE.sub("<NUM>", normalized)
    return normalized


def fingerprint(source: str | None, code: str | None, message: str | None) -> str:
    """A stable, PII-free grouping key for one error. `source`/`code`
    participate verbatim (they're Celigo-controlled enum-like values, never
    freeform PII-bearing text); `message` participates only through
    `normalize_message`. Two errors fingerprint identically iff they share
    `source`, `code`, and a message that's identical after normalization --
    e.g. differing only by order ref, customer email, digits, a UUID, or an
    ISO timestamp."""
    basis = f"{source or ''}|{code or ''}|{normalize_message(message)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _parse_timestamp(value: object) -> datetime | None:
    """Best-effort parse of a Celigo error timestamp (`occurredAt`/
    `purgeAt`) into an aware datetime. Mirrors `repository._parse_celigo_
    timestamp`'s defensive posture (an unparseable value degrades to NULL,
    never aborts the upsert) as an independent copy rather than an import --
    this module owns its own parsing of error-specific fields, the same way
    repository.py owns parsing of flow/script fields."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# upsert_errors() -- the sync entry point.
# ---------------------------------------------------------------------------


async def upsert_errors(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    step: CeligoFlowStep,
    raw_errors: Iterable[dict],
) -> None:
    """Snapshot *raw_errors* (one flow step's CURRENT open-error listing)
    into `celigo_error_signatures`/`celigo_flow_errors`.

    *step* is duck-typed: anything exposing `.id` (the `celigo_flow_steps`
    PK) and `.flow_id` (the `celigo_flows` PK) works -- a real
    `CeligoFlowStep` row is the natural choice, since a caller already has
    one in hand after Task 5's `sync_flow_steps`/`upsert_flow_step`.

    Never deletes a `celigo_flow_errors` row (see module docstring and
    `repository.py`'s own "never deleted" guarantee) -- an error missing
    from *raw_errors* that was previously open for this step is marked
    resolved, not removed.
    """
    raw_errors = list(raw_errors)
    sanitized_errors = [sanitize("error", raw) for raw in raw_errors]

    # Snapshot of what THIS STEP believed was open before this call writes
    # anything -- read first, so it isn't contaminated by this call's own
    # upserts.
    previously_open_ids = set(
        (
            await db.execute(
                select(CeligoFlowError.celigo_id).where(
                    CeligoFlowError.tenant_id == tenant_id,
                    CeligoFlowError.celigo_connection_id == connection_id,
                    CeligoFlowError.flow_step_id == step.id,
                    CeligoFlowError.resolved_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    # Group this call's errors by fingerprint. Errors with no `errorId` are
    # malformed -- skipped, never given a fabricated id (same posture as
    # `repository.extract_flow_steps` for a step with no export/import id).
    groups: dict[str, list[dict]] = defaultdict(list)
    now_open_ids: set[str] = set()
    for error in sanitized_errors:
        celigo_id = error.get("errorId")
        if not celigo_id:
            continue
        now_open_ids.add(celigo_id)
        fp = fingerprint(error.get("source"), error.get("code"), error.get("message"))
        groups[fp].append(error)

    # Preservation: anything this step had open before, that's absent from
    # *raw_errors* now, is resolved -- NEVER deleted. See module docstring
    # for why this stops at resolved_at and never touches purged_at.
    #
    # MUST run BEFORE phase 3's aggregate recompute below, not after -- see
    # module docstring's "ORDERING WITHIN ONE CALL" (FIX ROUND 1). Phase 3
    # filters on `resolved_at IS NULL`; resolving first means this call's
    # own resolutions are already reflected when that filter runs, instead
    # of leaving the count stale by exactly the set just resolved here.
    resolved_ids = previously_open_ids - now_open_ids
    if resolved_ids:
        await mark_flow_errors_resolved(db, tenant_id=tenant_id, connection_id=connection_id, celigo_ids=resolved_ids)

    # Phase 1: one signature row per fingerprint group, keyed by a
    # representative error's source/code/sample_message. Must happen BEFORE
    # phase 2 -- `celigo_flow_errors.signature_id` is a real (non-deferred)
    # FK, so the referenced row has to exist first.
    signature_ids: dict[str, uuid.UUID] = {}
    representatives: dict[str, dict] = {}
    for fp, group in groups.items():
        representative = group[0]
        representatives[fp] = representative
        signature_ids[fp] = await upsert_error_signature(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            fingerprint=fp,
            source=representative.get("source"),
            code=representative.get("code"),
            sample_message=representative.get("message"),
        )

    # Phase 2: one celigo_flow_errors row per error, referencing its group's
    # signature.
    for fp, group in groups.items():
        signature_id = signature_ids[fp]
        for error in group:
            await upsert_flow_error(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                celigo_id=error["errorId"],
                flow_id=step.flow_id,
                flow_step_id=step.id,
                signature_id=signature_id,
                trace_key=error.get("traceKey"),
                retry_data_key=error.get("retryDataKey"),
                source=error.get("source"),
                code=error.get("code"),
                message=error.get("message"),
                occurred_at=_parse_timestamp(error.get("occurredAt")),
                purge_at=_parse_timestamp(error.get("purgeAt")),
                flow_job_id=error.get("_flowJobId"),
                retriable=error.get("retriable"),
            )

    # Phase 3: recompute each touched signature's aggregate stats from the
    # actual stored rows (see module docstring -- never incremented in
    # place). Re-passes source/code/sample_message from phase 1 so this
    # second upsert doesn't clobber them back to NULL: `upsert_error_
    # signature` writes every field it's given unconditionally except
    # occurrence_count/first_seen/last_seen (which are conditional on not
    # being None), so this call must repeat the phase-1 identity fields to
    # be a safe idempotent re-assertion rather than a silent wipe.
    for fp, signature_id in signature_ids.items():
        count, first_seen, last_seen = (
            await db.execute(
                select(
                    func.count(CeligoFlowError.id).filter(CeligoFlowError.resolved_at.is_(None)),
                    func.min(CeligoFlowError.occurred_at),
                    func.max(CeligoFlowError.occurred_at),
                ).where(
                    CeligoFlowError.tenant_id == tenant_id,
                    CeligoFlowError.celigo_connection_id == connection_id,
                    CeligoFlowError.signature_id == signature_id,
                )
            )
        ).one()
        representative = representatives[fp]
        await upsert_error_signature(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            fingerprint=fp,
            source=representative.get("source"),
            code=representative.get("code"),
            sample_message=representative.get("message"),
            occurrence_count=count,
            first_seen=first_seen,
            last_seen=last_seen,
        )
