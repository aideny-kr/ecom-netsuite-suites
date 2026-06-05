# backend/app/services/metrics/metric_authoring.py
"""Author-time validation for metric definitions (one-of, key-allowlist, DAG, params)."""

import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.chat.domain_knowledge import embed_domain_query
from app.services.metrics.expression_evaluator import ExpressionError, extract_dependencies
from app.services.metrics.system_tenant import ensure_system_tenant

_SINGLE_SOURCE_KEYS = {"query", "dialect"}

# The ONLY param types that may flow into a filled blessed query. Free-text strings
# are excluded by design (§6 binding): an unconstrained string param would let
# arbitrary text into the SQL. `period` is allowed — it is server-resolved into
# date bounds, never a raw user string.
_ALLOWED_PARAM_TYPES = {"date", "int", "enum", "period"}

# Match a SQL bind placeholder (`:name`). Mirrors fill_query's substitution regex so
# the author-time cross-check sees exactly what the runtime filler will substitute.
_PLACEHOLDER_RE = re.compile(r":([a-zA-Z_]\w*)\b")


class AuthoringError(ValueError):
    pass


def _validate_params_schema(d: dict) -> None:
    """(b) Enforce the param-type allowlist and the two-way :name ↔ params_schema
    binding for query-backed metrics.

    - Every declared param type must be in {date,int,enum,period}; enum carries a
      non-empty `values` list.
    - Every :name placeholder in the blessed query is declared in params_schema.
    - Every declared NON-period param actually binds a placeholder in the query (no
      dead config). A `period` param is OPTIONALLY-binding (F4 (b)): it is server-
      resolved into :period_start / :period_end and carries no untrusted text, so a
      blessed query that declares `period` but does not (yet) slice by date — e.g. the
      seeded `SELECT 0` stubs — is valid. coerce_params still expands it when present.
      The strict bind-or-reject rule remains for date/int/enum params, where a declared
      param that never binds is genuinely dead config.
    """
    schema = d.get("params_schema") or {}

    # 1) type allowlist + enum values
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            raise AuthoringError(f"param '{name}' must be an object with a type")
        ptype = spec.get("type")
        if ptype not in _ALLOWED_PARAM_TYPES:
            raise AuthoringError(f"param '{name}' has type '{ptype}'; allowed types are {sorted(_ALLOWED_PARAM_TYPES)}")
        if ptype == "enum":
            values = spec.get("values")
            if not values:
                raise AuthoringError(f"enum param '{name}' must carry a non-empty values list")
            # F3 injection-hardening: enum `values` are the catalog's BLESSED set that flow
            # verbatim into the filled SQL at compute time. Reject any value carrying a
            # single quote, statement terminator (';'), line-comment ('--'), or BACKSLASH so
            # an injecty value can NEVER be persisted into a blessed metric row.
            #   - single quote / ';' / '--': classic SQL break-out / terminator / comment.
            #   - backslash: fill_query substitutes :name via re.sub, whose replacement
            #     TEMPLATE interprets backslash group-refs (`\g<0>` re-injects the matched
            #     placeholder text; `\1` raises re.error). The compute path renders via a
            #     callable replacement so this is inert at runtime, but a backslash value is
            #     still meaningless-as-data and an injection-shaped vector, so reject it here
            #     too (defense-in-depth: the value never reaches a blessed row).
            for v in values:
                sv = str(v)
                if "'" in sv or ";" in sv or "--" in sv or "\\" in sv:
                    raise AuthoringError(
                        f"enum param '{name}' has an unsafe value {v!r}: "
                        "values may not contain a single quote, ';', '--', or a backslash"
                    )

    # The :name binding check only applies to query-backed metrics (expression
    # metrics carry no blessed query).
    spec_obj = d.get("blessed_spec")
    if not isinstance(spec_obj, dict):
        return
    query = " ".join(str(v) for v in spec_obj.values() if isinstance(v, str))
    placeholders = set(_PLACEHOLDER_RE.findall(query))

    # 2) every placeholder is declared. A period param legitimately drives
    #    :period_start / :period_end, which are not literal schema keys.
    period_names = {n for n, s in schema.items() if isinstance(s, dict) and s.get("type") == "period"}
    expanded = set(schema) - period_names
    if period_names:
        expanded |= {"period_start", "period_end"}
    undeclared = placeholders - expanded
    if undeclared:
        raise AuthoringError(
            f"blessed query references undeclared params: {sorted(undeclared)} (declare them in params_schema)"
        )

    # 3) every declared NON-period param actually binds a placeholder (no dead config).
    #    period is OPTIONALLY-binding (F4 (b)): server-resolved + benign, so a declared
    #    period need not reference :period_start/:period_end (the seeded SELECT-0 stubs).
    for name in schema:
        if name in period_names:
            continue
        if name not in placeholders:
            raise AuthoringError(f"param '{name}' is declared but never referenced as :{name} in the query")


def validate_definition(d: dict, *, allowed_cross_source_keys: set[str] | None = None) -> None:
    kind = d.get("source_kind")
    spec, expr = d.get("blessed_spec"), d.get("expression")

    if bool(spec) == bool(expr):
        raise AuthoringError("exactly one of blessed_spec / expression must be set")

    if kind == "expression":
        if not expr or not d.get("depends_on"):
            raise AuthoringError("expression metrics need expression + depends_on")
        try:
            deps = set(extract_dependencies(expr))
        except ExpressionError as ex:
            raise AuthoringError(str(ex)) from ex
        if d["key"] in deps:
            raise AuthoringError("expression cannot reference itself (cycle)")
        if deps != set(d["depends_on"]):
            raise AuthoringError("depends_on must match expression references")
    else:
        if not isinstance(spec, dict):
            raise AuthoringError("query-backed metric needs a blessed_spec object")
        allowed = _SINGLE_SOURCE_KEYS if kind in ("suiteql", "bigquery") else (allowed_cross_source_keys or set())
        unknown = set(spec) - allowed
        if unknown:
            raise AuthoringError(f"blessed_spec has keys not in the live tool schema: {sorted(unknown)}")
        # Cross-check: if dialect is explicitly set, it must agree with source_kind.
        # Compute routes exclusively by source_kind; a contradictory dialect
        # (e.g. source_kind=bigquery + dialect=suiteql) signals a copy-paste error
        # that would silently compute via the wrong engine. A missing dialect (None)
        # is allowed — authors need not always set it.
        dialect = spec.get("dialect")
        if kind in ("suiteql", "bigquery") and dialect not in (None, kind):
            raise AuthoringError(f"blessed_spec dialect '{dialect}' must match source_kind '{kind}'")

    _validate_params_schema(d)


async def validate_leaves_exist(db: AsyncSession, *, tenant_id: uuid.UUID, d: dict) -> None:
    """(a) Author-time, DB-aware leaf-existence check for expression metrics.

    `validate_definition` proves `depends_on` matches the expression's references,
    but BOTH could name leaves that simply do not exist in the catalog. An expression
    metric persisted over phantom leaves would resolve to `missing_dependency` only at
    compute time — i.e. a blessed definition that can never produce a number. Worse,
    nothing stops authoring `net_margin = a / b` where `a`/`b` were never seeded, so
    the catalog silently advertises an un-computable metric.

    Query the depends_on keys over tenant ∪ SYSTEM (tenant-override-by-key already
    means presence in either scope satisfies the leaf) and reject (→ 422) if ANY leaf
    key is absent. Query-backed metrics have no leaves and are a no-op here.

    (F4 (a)) Filter status == 'active' so author-time leaf presence matches compute's
    active-only resolution (resolve_metric_by_key also filters status == 'active'). A
    leaf that exists but is needs_review/draft/deprecated resolves to None at compute
    and yields missing_dependency — so it must NOT count as present at author-time, else
    the catalog blesses an expression that can never compute.
    """
    if d.get("source_kind") != "expression":
        return
    deps = list(d.get("depends_on") or [])
    if not deps:
        return
    stmt = select(MetricDefinition.key, MetricDefinition.source_kind).where(
        or_(
            MetricDefinition.tenant_id == tenant_id,
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
        ),
        MetricDefinition.status == "active",
        MetricDefinition.key.in_(deps),
    )
    found = {k: sk for k, sk in (await db.execute(stmt)).all()}
    missing = [k for k in deps if k not in found]
    if missing:
        raise AuthoringError(
            f"expression references metric keys that do not exist in the catalog (tenant ∪ system): {sorted(missing)}"
        )
    non_query = [k for k, sk in found.items() if sk == "expression"]
    if non_query:
        raise AuthoringError(
            f"expression leaves must be query-backed, not expressions: {sorted(non_query)} "
            "(expressions are one-level; compose only over suiteql/bigquery metrics)"
        )


def _embed_text(payload: dict) -> str:
    parts = [payload.get("display_name", ""), payload.get("definition", "")]
    parts.extend(payload.get("synonyms") or [])
    return " | ".join(p for p in parts if p)


async def create_metric(db: AsyncSession, *, tenant_id: uuid.UUID, payload: dict) -> MetricDefinition:
    """Persist a tenant (or SYSTEM) metric definition with a 1536-d intent embedding."""
    # (a) DB-aware leaf-existence: an expression metric whose depends_on leaves are
    # not in the catalog (tenant ∪ SYSTEM) is blessed-but-un-computable. Reject BEFORE
    # any write so the anti-hallucination guarantee holds at author-time, not at the
    # eventual missing_dependency at compute.
    await validate_leaves_exist(db, tenant_id=tenant_id, d=payload)
    # Defense-in-depth so the authoring CLI is self-sufficient: a SYSTEM-default row
    # FKs to the synthetic SYSTEM tenant, which may not exist yet on a fresh DB.
    if tenant_id == SYSTEM_TENANT_ID:
        await ensure_system_tenant(db)
        await db.flush()
    embedding = await embed_domain_query(_embed_text(payload))
    metric = MetricDefinition(
        tenant_id=tenant_id,
        key=payload["key"],
        display_name=payload["display_name"],
        definition=payload["definition"],
        unit=payload["unit"],
        source_kind=payload["source_kind"],
        blessed_spec=payload.get("blessed_spec"),
        expression=payload.get("expression"),
        depends_on=payload.get("depends_on"),
        params_schema=payload.get("params_schema"),
        dimensions=payload.get("dimensions"),
        synonyms=payload.get("synonyms"),
        intent_embedding=embedding,
        status="active",
        version=1,
        provenance={"author": "tenant_admin"},
    )
    db.add(metric)
    await db.flush()
    return metric
