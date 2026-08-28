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
  * `upsert_errors(db, *, tenant_id, connection_id, step, raw_errors,
    raw_errors_is_complete)` --
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
that disappeared from *this step's* current listing AND was OPEN by
`models.celigo.celigo_error_is_open` -- an already-purged row is not
resolvable, because Celigo destroying a record is not this app observing it
resolve (FIX ROUND 9, re-review R3). It does NOT mark
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
until it resolves, sometimes over many days. Instead, `occurrence_count` is
recomputed as `COUNT(*) FILTER (WHERE resolved_at IS NULL)` and
`first_seen`/`last_seen` as `MIN`/`MAX(occurred_at)`, scoped to
`signature_id` (not to this call's step or even this call's flow), applied
via a plain `UPDATE` targeting that id -- never an upsert, because the
signature row is guaranteed to already exist (a `celigo_flow_errors` row
can only reference one through a real FK). This is idempotent by
construction and correctly reflects contributions from every OTHER step
that happens to share the same fingerprint, not just the step this call is
processing. `first_seen`/`last_seen` intentionally span resolved AND open
rows (the full historical range); `occurrence_count` intentionally counts
only currently-open rows (a resolved failure isn't still "occurring").

WHICH SIGNATURES GET RECOMPUTED -- MUST INCLUDE ORPHANS (FIX ROUND 2, team
lead, 2026-08-27 -- proven with a probe against shipped code, not a
reading): recomputing only the signatures that appear in THIS call's
`raw_errors` grouping is not enough. Take one signature with exactly one
open error, and a resync where that error is simply absent -- no sibling
under the same signature anywhere in the call, not on this step, not on
any other step this call happens to touch. Recomputing only from
`raw_errors` grouping never touches that signature at all, in EITHER
ordering, because it has zero representation to group by. `occurrence_
count` would freeze at its old value forever -- precisely the "genuinely
fixed, never seen again" case this module exists to get right. The fix:
before resolving, the pre-call snapshot query also captures each
about-to-be-resolved row's `signature_id` (not just its `celigo_id`), and
those ids are UNIONed into phase 3's recompute set alongside whatever came
from `raw_errors` grouping. A signature is recomputed because one of its
rows changed state THIS call -- a new/updated error OR a resolution --
never only because a sibling happened to appear in the same batch.

ORDERING WITHIN ONE CALL (FIX ROUND 1, team lead, 2026-08-27 -- caught by an
executed repro, not a reading): resolving must still happen BEFORE phase 3
recomputes, for the same reason as before -- the recompute filters `WHERE
resolved_at IS NULL`, so this call's own resolutions must already be
visible in the database when that filter runs, not applied afterward.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoErrorSignature, CeligoFlowError, CeligoFlowStep, celigo_error_is_open
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
    raw_errors_is_complete: bool,
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

    *raw_errors_is_complete* is REQUIRED -- keyword-only, no default (FIX
    ROUND 9, scoped re-review finding R1). Resolving an error absent from
    *raw_errors* is only correct if *raw_errors* is the step's WHOLE current
    open-error listing, never a partial page of it:

      * `True` -- *raw_errors* is the complete current listing. Anything this
        step had open that is absent from it is resolved.
      * `False` -- *raw_errors* is known-partial. NO previously-open error is
        resolved this call; new/updated errors in *raw_errors* are still
        recorded and their signatures' stats still recomputed normally.

    FIX ROUND 8 (whole-branch review finding 4) added this parameter with a
    `True` default, and the re-review then reproduced finding 4's original
    bug straight THROUGH it: a caller that merely omitted the argument got
    the old silent-resolution behaviour back. A guard a caller has to
    remember is not a guard. With no default, omitting it is a `TypeError`
    at the call site -- the same shape `repository.upsert_script_attachment`
    already uses for `reference_object_celigo_id`, and the reason "forgot to
    say whether the list was whole" is no longer expressible rather than
    merely discouraged.
    """
    raw_errors = list(raw_errors)
    sanitized_errors = [sanitize("error", raw) for raw in raw_errors]

    # Snapshot of every row THIS STEP could still transition this call --
    # read first, so it isn't contaminated by this call's own upserts.
    # Captures each row's `signature_id` too, not just its `celigo_id` --
    # FIX ROUND 2: a resolved row's signature must be recomputed even when it
    # has ZERO representation anywhere in *raw_errors* (no sibling on this
    # step or any other step in this call). See module docstring's "WHICH
    # SIGNATURES GET RECOMPUTED".
    #
    # `resolved_at IS NULL` here is the precondition for a row being IN PLAY
    # (already-resolved rows are done), NOT a definition of "open" -- FIX
    # ROUND 9, re-review R3. It used to be both, which made it a third,
    # divergent open-predicate inside the module that imports the canonical
    # one: a row Celigo had already PURGED counted as previously open, so its
    # absence from the next listing stamped it with a `resolved_at` this app
    # never observed. Openness is decided below by `celigo_error_is_open()`
    # and nothing else.
    previously_unresolved_rows = (
        await db.execute(
            select(
                CeligoFlowError.celigo_id,
                CeligoFlowError.signature_id,
                celigo_error_is_open().label("is_open"),
            ).where(
                CeligoFlowError.tenant_id == tenant_id,
                CeligoFlowError.celigo_connection_id == connection_id,
                CeligoFlowError.flow_step_id == step.id,
                CeligoFlowError.resolved_at.is_(None),
            )
        )
    ).all()
    previously_open_signature_by_id: dict[str, uuid.UUID | None] = {
        row.celigo_id: row.signature_id for row in previously_unresolved_rows if row.is_open
    }
    previously_open_ids = set(previously_open_signature_by_id)

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
    # GATED ON raw_errors_is_complete (FIX ROUND 8, finding 4; made
    # unforgettable in FIX ROUND 9, re-review R1): resolving an absence is
    # only correct if *raw_errors* is provably the WHOLE current listing --
    # a caller that admits it handed over a partial one must never have that
    # partiality read back as "these errors are gone now". The parameter has
    # no default, so "admits" is the only option: every caller states it.
    #
    # MUST run BEFORE phase 3's aggregate recompute below, not after -- see
    # module docstring's "ORDERING WITHIN ONE CALL" (FIX ROUND 1). Phase 3
    # filters on `resolved_at IS NULL`; resolving first means this call's
    # own resolutions are already reflected when that filter runs, instead
    # of leaving the count stale by exactly the set just resolved here.
    resolved_ids: set[str] = previously_open_ids - now_open_ids if raw_errors_is_complete else set()
    resolved_signature_ids = {
        previously_open_signature_by_id[celigo_id]
        for celigo_id in resolved_ids
        if previously_open_signature_by_id[celigo_id] is not None
    }

    # A row that is unresolved but already PURGED gets no `resolved_at` (see
    # the snapshot query's comment) -- but its signature's stored
    # `occurrence_count` may still be stale, because
    # `repository.mark_flow_errors_purged` sets one column and recomputes
    # nothing. Before FIX ROUND 9 that staleness happened to be repaired as a
    # SIDE EFFECT of the wrong resolution putting the signature in the
    # recompute set; removing the wrong resolution must not take the repair
    # with it. Its signature is recomputed on the same trigger as any other:
    # one of its rows changed state (Celigo destroyed it) and it is no longer
    # listed.
    purged_and_absent_signature_ids = {
        row.signature_id
        for row in previously_unresolved_rows
        if not row.is_open and row.signature_id is not None and row.celigo_id not in now_open_ids
    }
    if resolved_ids:
        await mark_flow_errors_resolved(db, tenant_id=tenant_id, connection_id=connection_id, celigo_ids=resolved_ids)

    # Phase 1: one signature row per fingerprint group, keyed by a
    # representative error's source/code/sample_message. Must happen BEFORE
    # phase 2 -- `celigo_flow_errors.signature_id` is a real (non-deferred)
    # FK, so the referenced row has to exist first.
    signature_ids: dict[str, uuid.UUID] = {}
    for fp, group in groups.items():
        representative = group[0]
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

    # Phase 3: recompute every TOUCHED signature's aggregate stats from the
    # actual stored rows (see module docstring -- never incremented in
    # place). "Touched" is the union of two sets, not just the first one --
    # FIX ROUND 2: a resolved row's signature must be recomputed even with
    # zero representation in *raw_errors* (see module docstring's "WHICH
    # SIGNATURES GET RECOMPUTED"):
    #   * signature_ids.values() -- signatures with a live error in THIS
    #     call's raw_errors grouping (phases 1-2 above).
    #   * resolved_signature_ids -- signatures of rows resolved above,
    #     whether or not they have any sibling in this call.
    # A plain UPDATE by id, not an upsert: the row is guaranteed to already
    # exist (celigo_flow_errors.signature_id is a real FK into it), so
    # there's no insert case to handle and no need to re-supply
    # source/code/sample_message -- those identity fields are set once, by
    # phase 1, and never touched here.
    touched_signature_ids = set(signature_ids.values()) | resolved_signature_ids | purged_and_absent_signature_ids
    for signature_id in touched_signature_ids:
        count, first_seen, last_seen = (
            await db.execute(
                select(
                    # FIX ROUND 8 (whole-branch review finding 5): this used
                    # to filter `resolved_at IS NULL` alone, disagreeing with
                    # `celigo_flows.py`'s open-count query (which also
                    # excludes `purged_at IS NOT NULL` rows) -- see
                    # `celigo_error_is_open`'s own docstring for why they
                    # must agree and why it lives there, not here.
                    func.count(CeligoFlowError.id).filter(celigo_error_is_open()),
                    func.min(CeligoFlowError.occurred_at),
                    func.max(CeligoFlowError.occurred_at),
                ).where(
                    CeligoFlowError.tenant_id == tenant_id,
                    CeligoFlowError.celigo_connection_id == connection_id,
                    CeligoFlowError.signature_id == signature_id,
                )
            )
        ).one()
        await db.execute(
            update(CeligoErrorSignature)
            .where(
                CeligoErrorSignature.tenant_id == tenant_id,
                CeligoErrorSignature.celigo_connection_id == connection_id,
                CeligoErrorSignature.id == signature_id,
            )
            .values(occurrence_count=count, first_seen=first_seen, last_seen=last_seen, updated_at=func.now())
        )
