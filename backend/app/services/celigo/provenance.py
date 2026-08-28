"""Task 11: derive which flows write which NetSuite record types, from
already-synced flow-step provenance columns (Task 7 fix round 2, migration
096) -- see `app/models/celigo.py`'s `CeligoFlowStep` docstring and
`app/services/celigo/sync_service.py`'s `_extract_provenance` for where
`record_type`/`operation`/`search_id` come from and how they're backfilled.

SCOPE: WRITES ONLY. `operation` is populated exclusively from
`netsuite_da.operation` (imports) -- see the model's and sync_service's own
docstrings -- so a non-NULL `operation` is, by construction, the write
signal. Export steps carry `record_type`/`search_id` (from
`netsuite.restlet`) but never `operation`; that is the READ side of the same
account (a saved search Celigo runs against NetSuite to pull data out, not
to write it), and it is deliberately excluded here rather than blurred into
one list -- the team lead's brief for this task ("do not blur the two into
one list") and the plan's own framing ("the plan's goal is WRITES") are both
explicit about this. A future task wanting export/read provenance gets its
own function, not a flag bolted onto this one.

THE COMMON CASE IS NO PROVENANCE AT ALL. observed-shapes.md (probed live,
2026-08-27): 25 sampled real exports were AS2Export/FTPExport/NetSuiteExport
-- only NetSuite-adaptor steps carry `netsuite_da`/`netsuite` at all;
everything else has NULL in `record_type`/`operation`/`search_id`. This
module treats that as the normal case (silently excluded from the result,
never guessed at) -- see `derive_flow_record_writes`'s own docstring.

Config-derived only, same discipline as the rest of this package: no
inference, no heuristics on flow/step names, no model. If the synced config
doesn't carry a value, this module has nothing to say about it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlow, CeligoFlowStep


@dataclass(frozen=True)
class FlowRecordWrite:
    """One write relationship: flow step *flow_step_id* (inside flow
    *flow_id*) writes NetSuite record type *record_type* via *operation*
    (Celigo's own `netsuite_da.operation` vocabulary, e.g. "update"/"add" --
    passed through verbatim, never interpreted or normalized). One instance
    per contributing `celigo_flow_steps` row: a flow with three import steps
    that each write a different record type produces three of these, not
    one merged entry -- see `group_by_flow`/`group_by_record_type` below for
    the two natural groupings of the flat list a consumer is more likely to
    want than the raw rows."""

    flow_id: uuid.UUID
    flow_celigo_id: str
    flow_name: str
    flow_step_id: uuid.UUID
    record_type: str
    operation: str


async def derive_flow_record_writes(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> list[FlowRecordWrite]:
    """Every `(flow, record_type, operation)` write relationship visible in
    already-synced config for one connection.

    Tenant-scoped EXPLICITLY on both sides of the join (`CeligoFlowStep.
    tenant_id` and `CeligoFlow.tenant_id`) -- RLS is a backstop here, not the
    control: the test harness runs as a superuser, under which RLS protects
    nothing, so an explicit predicate is the only thing that actually keeps
    one tenant's rows out of another's result.

    Reads ONLY `celigo_flow_steps.record_type`/`.operation` (never
    `raw_json`, never re-derives anything from the original wire shape),
    joined to the owning `celigo_flows` row for `celigo_id`/`name`. A row is
    included only when BOTH `record_type` and `operation` are non-NULL:
      * `operation IS NULL` alone already excludes both the non-NetSuite
        case (all three provenance columns NULL -- the COMMON case, per
        module docstring) and the export/read case (`record_type`/
        `search_id` set, `operation` NULL), because `operation` is only
        ever populated from the import side.
      * `record_type IS NULL` is excluded too, defensively, even though
        `_extract_provenance` (sync_service.py) always sets `record_type`
        and `operation` together from the same `netsuite_da` object -- this
        function never fabricates a record type for a row it can't fully
        account for.

    No result for a flow means one of two things this function cannot tell
    apart on its own: the flow's steps genuinely carry no NetSuite write
    (e.g. it's all EDI adaptors), or its steps simply haven't been synced
    yet. A caller that needs to distinguish those should check
    `celigo_flow_steps` directly for that flow -- this function only reports
    what config says, never guesses in either direction."""
    rows = (
        await db.execute(
            select(
                CeligoFlowStep.id,
                CeligoFlowStep.flow_id,
                CeligoFlowStep.record_type,
                CeligoFlowStep.operation,
                CeligoFlow.celigo_id,
                CeligoFlow.name,
            )
            .join(CeligoFlow, CeligoFlow.id == CeligoFlowStep.flow_id)
            .where(
                CeligoFlowStep.tenant_id == tenant_id,
                CeligoFlowStep.celigo_connection_id == connection_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlowStep.record_type.isnot(None),
                CeligoFlowStep.operation.isnot(None),
            )
            .order_by(CeligoFlowStep.flow_id, CeligoFlowStep.sequence)
        )
    ).all()

    return [
        FlowRecordWrite(
            flow_id=row.flow_id,
            flow_celigo_id=row.celigo_id,
            flow_name=row.name,
            flow_step_id=row.id,
            record_type=row.record_type,
            operation=row.operation,
        )
        for row in rows
    ]


def group_by_record_type(writes: list[FlowRecordWrite]) -> dict[str, list[FlowRecordWrite]]:
    """Pure regroup of `derive_flow_record_writes`' flat output: "which flows
    write record type X" -- the reverse index a consumer diagnosing an
    incident against one NetSuite record type (e.g. "what could have written
    to returnauthorization") is most likely to want. No DB access; operates
    on an already-derived list."""
    out: dict[str, list[FlowRecordWrite]] = defaultdict(list)
    for write in writes:
        out[write.record_type].append(write)
    return dict(out)


def group_by_flow(writes: list[FlowRecordWrite]) -> dict[uuid.UUID, list[FlowRecordWrite]]:
    """Pure regroup of `derive_flow_record_writes`' flat output: "which
    record types does flow Y write" -- the forward index a consumer already
    looking at one flow (e.g. a flow-detail screen) is most likely to want.
    No DB access; operates on an already-derived list."""
    out: dict[uuid.UUID, list[FlowRecordWrite]] = defaultdict(list)
    for write in writes:
        out[write.flow_id].append(write)
    return dict(out)
