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
    stmt = select(MetricDefinition.key, MetricDefinition.source_kind, MetricDefinition.tenant_id).where(
        or_(
            MetricDefinition.tenant_id == tenant_id,
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
        ),
        MetricDefinition.status == "active",
        MetricDefinition.key.in_(deps),
    )
    # NEW-5: apply tenant-wins-by-key so the source_kind seen here matches what
    # resolve_metric_by_key (compute) would resolve. A tenant row always overrides a SYSTEM
    # row for the same key — the same precedence as compute's `tenant_row or system_row`.
    # Without this, a {k: sk for k, sk in rows} dict picks whichever row the DB returns
    # last for a duplicate key (non-deterministic), potentially seeing the SYSTEM row's
    # source_kind while compute resolves the tenant row — a blessed-but-uncomputable metric.
    found: dict[str, str] = {}
    for row_key, row_sk, row_tid in (await db.execute(stmt)).all():
        if row_key not in found or row_tid == tenant_id:
            found[row_key] = row_sk
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


async def _check_reverse_dependencies(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
) -> None:
    """M3: reverse-dependency guard for update_metric.

    If an edit would change a query-backed leaf's `source_kind` to `'expression'` OR
    change its `status` away from `'active'`, that leaf would disappear from compute's
    active-only resolution (resolve_metric_by_key filters `status == 'active'` and
    rejects expression metrics used as leaves).  Any active expression metric that
    currently depends on the edited key would then return `missing_dependency` at compute
    time — silently breaking the catalog with no author-time warning.

    Query the catalog for active expression metrics across tenant ∪ SYSTEM whose
    `depends_on` array contains `key`. If any are found, raise AuthoringError naming them.

    Only called when the edit changes `source_kind` to `'expression'` OR changes `status`
    away from `'active'` (harmless edits like display_name never trigger this).
    """
    stmt = select(MetricDefinition.key).where(
        or_(
            MetricDefinition.tenant_id == tenant_id,
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
        ),
        MetricDefinition.source_kind == "expression",
        MetricDefinition.status == "active",
        # PostgreSQL @> operator: array contains the given element.
        MetricDefinition.depends_on.contains([key]),
    )
    dependent_keys = list((await db.execute(stmt)).scalars().all())
    if dependent_keys:
        raise AuthoringError(
            f"cannot edit '{key}': active expression metric(s) depend on it as a "
            f"query-backed leaf: {sorted(dependent_keys)}"
        )


async def update_metric(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metric_id: uuid.UUID,
    payload: dict,
) -> MetricDefinition:
    """Edit a definition: re-validate, bump version, allow status transitions (incl.
    reactivating a draft/needs_review row). Tenant-scoped: a tenant may only update its
    own rows; SYSTEM rows update under SYSTEM_TENANT_ID via the superadmin route."""
    # NEW-2 (lost-update race): SELECT … FOR UPDATE serialises concurrent PUTs on the
    # same row. Without the lock two concurrent requests both read version N, both write
    # N+1 — one update is silently lost. WITH FOR UPDATE the second transaction blocks
    # until the first commits, then reads the post-commit version (N+1) and writes N+2.
    metric = (
        await db.execute(
            select(MetricDefinition)
            .where(
                MetricDefinition.id == metric_id,
                MetricDefinition.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if metric is None:
        raise AuthoringError("metric not found for this tenant")

    # R4: source_kind is immutable. A raw-dict caller can include source_kind in the
    # payload; the _MERGE_FIELDS merge would feed it into validate_definition (so the
    # merge is validated as the NEW engine), but the apply loop below deliberately omits
    # source_kind — so the row would stay persisted as the OLD engine. That is a silent
    # validate-as-X persist-as-Y drift. Reject the moment a differing value is seen,
    # BEFORE any validation runs, so the inconsistency can never be silently accepted.
    # A payload with no source_kind OR one equal to the existing value is harmless.
    _payload_source_kind = payload.get("source_kind")
    if _payload_source_kind is not None and _payload_source_kind != metric.source_kind:
        raise AuthoringError(
            f"source_kind is immutable; cannot change after creation "
            f"(existing: '{metric.source_kind}', requested: '{_payload_source_kind}')"
        )

    # M3: reverse-dependency guard. If the edit changes source_kind to 'expression'
    # (making it no longer a valid query-backed leaf) OR changes status away from
    # 'active' (making it invisible to compute's active-only leaf resolver), any active
    # expression metric that currently depends on this key would silently break at compute
    # time. Reject BEFORE applying the edit so the catalog invariant is maintained.
    _new_source_kind = payload.get("source_kind")
    _new_status = payload.get("status")
    _edit_breaks_leaf = (_new_source_kind is not None and _new_source_kind == "expression") or (
        _new_status is not None and _new_status != "active"
    )
    if _edit_breaks_leaf:
        await _check_reverse_dependencies(db, tenant_id=tenant_id, key=metric.key)

    # Merge current values with the incoming patch (exclude None so unset fields keep
    # the existing value). synonyms is carried so the embedding recompute below sees the
    # row's current synonyms even when this PUT only touches display_name/definition;
    # validate_definition ignores synonyms, so its presence in merged is inert there.
    _MERGE_FIELDS = (
        "key",
        "display_name",
        "definition",
        "unit",
        "format",
        "source_kind",
        "blessed_spec",
        "expression",
        "depends_on",
        "params_schema",
        "dimensions",
        "synonyms",
    )
    merged = {c: getattr(metric, c) for c in _MERGE_FIELDS}
    merged.update({k: v for k, v in payload.items() if v is not None and k != "status"})

    # Defensive invariant (R4): after the immutability guard above, the merged
    # source_kind must equal the existing metric.source_kind — validate_definition must
    # always see the engine that will actually execute the query. Assert explicitly so
    # a future refactor that touches _MERGE_FIELDS or the merge logic cannot silently
    # reintroduce validate-as-X persist-as-Y drift.
    assert merged["source_kind"] == metric.source_kind, (
        f"BUG: merged source_kind '{merged['source_kind']}' != metric.source_kind "
        f"'{metric.source_kind}' after immutability guard — this is a programming error"
    )

    validate_definition(merged)
    await validate_leaves_exist(db, tenant_id=tenant_id, d=merged)

    # Apply mutable fields (excluding key — key is the stable identity of a metric).
    # synonyms is intentionally included: create_metric persists it, so an edit must
    # too (else a client-supplied synonyms patch is silently dropped on update).
    for field in (
        "display_name",
        "definition",
        "unit",
        "format",
        "blessed_spec",
        "expression",
        "depends_on",
        "params_schema",
        "dimensions",
        "synonyms",
        "status",
    ):
        if payload.get(field) is not None:
            setattr(metric, field, payload[field])

    # NEW-3 (reactivation smoke): when the resulting status is 'active' AND the metric
    # is query-backed (suiteql or bigquery), validate the blessed query is read-only AND
    # on the allowlist BEFORE allowing activation. This prevents a broken/unsafe query
    # from sitting in the catalog as 'active' (where a subsequent compute call would
    # attempt to execute it). We validate the TEMPLATE as-is — do NOT execute it.
    resulting_status = payload.get("status") or metric.status
    if resulting_status == "active" and metric.source_kind in ("suiteql", "bigquery"):
        _validate_blessed_query_for_activation(metric.source_kind, metric.blessed_spec)

    # Keep the intent embedding in sync with the embedded text. create_metric derives
    # it from display_name | definition | synonyms via _embed_text; if any of those
    # changed on this PUT the stored vector is now stale and resolve would match on
    # out-of-date text, so recompute it from the MERGED definition. Tolerate a None
    # embedding exactly as create_metric does (embed_domain_query may return None).
    if any(payload.get(f) is not None for f in ("display_name", "definition", "synonyms")):
        _new_embedding = await embed_domain_query(_embed_text(merged))
        if _new_embedding is not None and len(_new_embedding) != 1536:
            raise AuthoringError("intent embedding must be 1536-d (use embed_domain_*)")
        # Intentional: None means the embedding provider is unconfigured. The metric
        # becomes keyword-only searchable (vector similarity disabled). Not an error.
        if _new_embedding is None:
            print(
                f"[metric_authoring] WARNING: embed_domain_query returned None on update "
                f"for key='{metric.key}' — metric will be keyword-only searchable "
                f"(intent_embedding=None). Configure the embedding provider to enable "
                f"vector similarity routing.",
                flush=True,
            )
        metric.intent_embedding = _new_embedding

    metric.version += 1

    # B2 + NEW-6 (provenance stamp): only convert 'system_seed' → 'authored' so the
    # nightly seeder's conditional-upsert guard (author=='system_seed' → overwrite) does
    # NOT clobber a superadmin's/tenant_admin's edit on the next run. Any other author
    # class (e.g. 'superadmin', 'tenant_admin', 'authored') is PRESERVED — unconditionally
    # clobbering to 'authored' destroys the real author class carried by non-seeded rows.
    # Always set updated_via='api' for audit context (NEW-6).
    prov = {**(metric.provenance or {})}
    if prov.get("author") == "system_seed":
        prov["author"] = "authored"  # B2: mark non-seeder-owned so reseed skips it
    prov["updated_via"] = "api"
    metric.provenance = prov

    await db.flush()
    return metric


def _validate_blessed_query_for_activation(source_kind: str, blessed_spec: dict | None) -> None:
    """NEW-3: read-only + allowlist smoke check run BEFORE activating a query-backed
    metric. Validates the TEMPLATE query (with :param placeholders intact — do NOT
    execute). Raises AuthoringError if the query is DML/DDL or references off-allowlist
    tables. Reuses the same validators as metric_compute._validate_and_execute_by_source
    so the author-time gate and the compute gate agree on what is permitted.

    Called only when source_kind in ('suiteql', 'bigquery') and resulting_status == 'active'.
    """
    if not isinstance(blessed_spec, dict):
        raise AuthoringError("cannot activate: metric has no blessed_spec")
    query = blessed_spec.get("query", "")
    if not query:
        raise AuthoringError("cannot activate: blessed_spec has no query")

    if source_kind == "bigquery":
        from app.services.bigquery_service import _validate_read_only

        try:
            _validate_read_only(query)
        except ValueError as ex:
            raise AuthoringError(f"cannot activate: blessed query failed read-only validation: {ex}") from ex
        # Dataset allowlist (mirrors metric_compute._validate_and_execute_by_source).
        import re as _re

        from app.core.config import settings

        allowed = {
            d.strip().lower() for d in getattr(settings, "BIGQUERY_ALLOWED_DATASETS", "").split(",") if d.strip()
        }
        if allowed:
            used: set[str] = set()
            for ref in _re.findall(r"(?:FROM|JOIN)\s+([`A-Za-z0-9_.\-]+)", query, _re.IGNORECASE):
                parts = [p.strip("`") for p in ref.strip("`").split(".") if p.strip("`")]
                if len(parts) >= 2:
                    used.add(parts[-2].lower())
            illegal = used - allowed
            if illegal:
                raise AuthoringError(
                    f"cannot activate: blessed query selects off-allowlist datasets: {sorted(illegal)}"
                )
    else:
        # suiteql (default)
        from app.core.config import settings
        from app.mcp.tools import netsuite_suiteql

        allowed_tables = {t.strip().lower() for t in settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")}
        try:
            netsuite_suiteql.validate_query(query, allowed_tables)
        except ValueError as ex:
            raise AuthoringError(f"cannot activate: blessed query failed read-only/allowlist validation: {ex}") from ex


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
    if embedding is not None and len(embedding) != 1536:
        raise AuthoringError("intent embedding must be 1536-d (use embed_domain_*)")
    # Intentional: a None embedding means the embedding provider is unconfigured (no
    # OPENAI_API_KEY / vector backend). The row is persisted with intent_embedding=None
    # and the metric becomes keyword-only searchable (vector similarity disabled).
    # This is acceptable for local / CI environments — not a silent error.
    if embedding is None:
        print(
            f"[metric_authoring] WARNING: embed_domain_query returned None for key="
            f"'{payload.get('key')}' — metric will be keyword-only searchable "
            f"(intent_embedding=None). Configure the embedding provider to enable "
            f"vector similarity routing.",
            flush=True,
        )
    metric = MetricDefinition(
        tenant_id=tenant_id,
        key=payload["key"],
        display_name=payload["display_name"],
        definition=payload["definition"],
        unit=payload["unit"],
        source_kind=payload["source_kind"],
        format=payload.get("format"),
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
