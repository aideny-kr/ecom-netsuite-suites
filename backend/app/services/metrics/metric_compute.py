# backend/app/services/metrics/metric_compute.py
"""Deterministic execution of a metric: coerce params, fill blessed query, execute, shape as data_table."""

import re
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.metrics.expression_evaluator import ExpressionError, evaluate_expression
from app.services.metrics.period_resolver import PeriodError, resolve_period


class ParamError(ValueError):
    pass


class ComputeError(RuntimeError):
    """A blessed query failed or returned an unusable result.

    Raised instead of fabricating a value (never `or [[0]]`). compute_metric
    catches this, audit-logs the failure via _log_compute_failure, and returns a
    number-free error dict — NEVER mutating the definition (D1: compute is
    read-only). Honors the anti-hallucination invariant that a failed query must
    NEVER surface a wrong/zero number.
    """

    pass


def coerce_params(
    params_schema: dict,
    params: dict,
    *,
    fiscal_year_start_month: int = 1,
    today: date | None = None,
) -> dict:
    schema = params_schema or {}
    for name in params:
        if name not in schema:
            raise ParamError(f"unknown param: {name}")
    out: dict = {}
    for name, spec in schema.items():
        ptype = spec.get("type")
        if ptype == "period":
            token = params.get(name, "this_month")
            s, e = resolve_period(
                token,
                fiscal_year_start_month=fiscal_year_start_month,
                today=today or date.today(),
            )
            out["period_start"] = s.isoformat()
            out["period_end"] = e.isoformat()
            continue
        if name not in params:
            raise ParamError(f"missing param: {name}")
        val = params[name]
        if ptype == "int":
            try:
                out[name] = int(val)
            except (TypeError, ValueError) as ex:
                raise ParamError(f"{name} must be int") from ex
        elif ptype == "date":
            try:
                out[name] = datetime.strptime(str(val), "%Y-%m-%d").date().isoformat()
            except ValueError as ex:
                raise ParamError(f"{name} must be YYYY-MM-DD") from ex
        elif ptype == "enum":
            if val not in spec.get("values", []):
                raise ParamError(f"{name} not in allowed values")
            out[name] = val
        else:
            raise ParamError(f"unsupported param type: {ptype}")
    return out


def fill_query(query: str, coerced: dict) -> str:
    def _render(v) -> str:
        if isinstance(v, int):
            return str(v)
        # F3 injection-hardening: a string literal value (enum/date) is rendered INSIDE
        # single quotes. Double every embedded single quote (SQL-standard escaping) so a
        # value like `x' OR '1'='1` stays one inert literal instead of breaking out into
        # SQL control. This is the compute-path second line of defense behind the
        # author-time enum-value rejection in metric_authoring._validate_params_schema.
        return "'" + str(v).replace("'", "''") + "'"

    # Validate against the TEMPLATE, not the filled output. The old guard scanned the
    # FILLED string for a residual `:word`, which a legitimately-rendered string value
    # containing a colon (e.g. an enum value 'EU:Q1') falsely tripped — making a blessed
    # metric + a valid enum value silently un-computable. Instead require every :name
    # placeholder in the ORIGINAL query to be provided by `coerced` (the only safe
    # substitution source); rendered values can never introduce a phantom placeholder.
    template_placeholders = set(re.findall(r":([a-zA-Z_]\w*)\b", query))
    if template_placeholders - set(coerced):
        raise ParamError("unfilled placeholder remains")

    filled = query
    for name, val in coerced.items():
        # F3 injection-hardening: pass _render(val) via a CALLABLE replacement, not as a
        # replacement-string template. re.sub interprets backslash sequences (`\1`,
        # `\g<0>`, `\g<name>`, `\\`) in a STRING replacement — so a value like `us\g<0>`
        # would re-inject the matched placeholder text into the SQL, and `a\1b` would
        # raise an uncaught re.error (escaping the backslash, fail-closed). A callable
        # replacement is inserted VERBATIM, with no template interpretation, so the
        # rendered (quote-escaped) literal lands exactly as data regardless of backslashes.
        rendered = _render(val)
        filled = re.sub(rf":{re.escape(name)}\b", lambda _m, _r=rendered: _r, filled)
    return filled


def metric_data_table(
    display_name: str,
    value,
    unit: str,
    period_label: str,
    query_label,
    *,
    definition_version: int | None = None,
    source_kind: str | None = None,
) -> dict:
    # F4 (c): the `query` field is copied verbatim into the data_table SSE payload that
    # reaches the FRONTEND. It MUST be a plain string label (the metric key), NOT the
    # internal blessed_spec dict — exposing blessed_spec would ship the raw blessed
    # SuiteQL/BigQuery text (table names, dialect) to the client. Coerce defensively so a
    # dict can never land here even if a caller regresses.
    label = query_label if isinstance(query_label, str) else ""
    payload: dict = {
        "columns": ["Metric", "Value", "Unit", "Period"],
        "rows": [[display_name, value, unit, period_label]],
        "row_count": 1,
        "query": label,
        "truncated": False,
        # Trust boundary: the whole table is ONE computed number. The orchestrator's
        # data_table interception must render it on the frontend but withhold the
        # value from the LLM-facing condensed string (anti-hallucination invariant).
        "suppress_llm_value": True,
    }
    # §10 audit-citation: include the definition version that produced this number so
    # downstream consumers (SSE renderer, audit trail) can attribute the number to the
    # exact definition version. Only set when the caller supplies the version (the
    # production call site always does; test helpers that don't care may omit it).
    if definition_version is not None:
        payload["definition_version"] = definition_version
    # M4: expose source_kind so the orchestrator's source-pin logic can distinguish a
    # BigQuery metric from a SuiteQL/NetSuite one (without it the orchestrator wrongly
    # pins NetSuite for every metric). Only set when the caller supplies it.
    if source_kind is not None:
        payload["source_kind"] = source_kind
    return payload


def is_suppressed_metric_payload(parsed: object) -> bool:
    """True iff `parsed` is a metric data_table that opted into value suppression.

    The single predicate both the streaming interceptor (orchestrator) and the
    non-streaming agent path use to recognize a metric trust-boundary payload, so
    the two paths cannot drift on what counts as 'a number to withhold'.
    """
    return isinstance(parsed, dict) and parsed.get("suppress_llm_value") is True


def condense_metric_for_llm(parsed: dict) -> str:
    """Return the LLM-facing condensed string for a suppressed metric payload:
    shape + a do-not-recompute note, but NO value/rows. This is the ONE definition
    of the metric trust boundary's LLM-facing view — reused by both the streaming
    interceptor and the non-streaming run() path so a metric number can never reach
    the model on either path (anti-hallucination invariant)."""
    import json as _json

    return _json.dumps(
        {
            "row_count": parsed.get("row_count"),
            "columns": parsed.get("columns"),
            "note": (
                "1-row metric table rendered on the frontend. "
                "Do NOT state or recompute the number; provide commentary only."
            ),
        },
        default=str,
    )


async def _validate_and_execute_by_source(db, tenant_id, source_kind: str, query: str) -> dict:
    """Route the FILLED blessed query to the executor for its source_kind, applying
    THAT tool's own read-only validation before execution. Hardcoding one tool would
    run a bigquery metric's query against NetSuite (wrong data source) — surfacing a
    number from the wrong system under the catalog's authority. Each branch validates
    with the dialect-correct read-only check (SuiteQL vs BigQuery SQL differ).
    """
    if source_kind == "bigquery":
        from app.core.config import settings
        from app.mcp.tools import bigquery_tools
        from app.services.bigquery_service import _strip_sql_comments, _validate_read_only

        try:
            _validate_read_only(query)
        except ValueError as ex:
            # F4 (d): symmetric with the suiteql branch below — a FILLED blessed query
            # that fails read-only re-validation is a spec/schema-drift condition, NOT a
            # caller param error. Raise ComputeError so compute_metric's uniform handler
            # audit-logs the failure and returns a number-free dict (compute is read-only).
            raise ComputeError(f"filled bigquery query failed read-only validation: {ex}") from ex
        # R1#7: symmetric dataset-allowlist enforcement (mirrors SuiteQL table-allowlist
        # in netsuite_suiteql.parse_tables — same (?:FROM|JOIN) + re.IGNORECASE shape,
        # adapted for BigQuery's dotted namespace). Empty setting = no restriction
        # (backward compatible).
        allowed = {
            d.strip().lower() for d in getattr(settings, "BIGQUERY_ALLOWED_DATASETS", "").split(",") if d.strip()
        }
        if allowed:
            # Match FROM and JOIN (case-insensitive). A BigQuery ref is `dataset.table` or
            # `project.dataset.table` (project ids may contain hyphens; any part may be
            # backtick-quoted). The DATASET is the SECOND-to-last dotted component — for a
            # 2-part ref that is the first part, for a 3-part ref the middle part (NOT the
            # project). Extracting the wrong part both over-blocks legit cross-project
            # queries and under-blocks `allowed.secret.t` style attacks.
            # Strip SQL comments BEFORE the dataset regex: a comment between FROM/JOIN
            # and the table name (`FROM /*x*/ secret.t`) otherwise makes the regex
            # capture nothing, so an off-allowlist dataset slips past the gate.
            decommented = _strip_sql_comments(query)
            used: set[str] = set()
            for ref in re.findall(r"(?:FROM|JOIN)\s+([`A-Za-z0-9_.\-]+)", decommented, re.IGNORECASE):
                parts = [p.strip("`") for p in ref.strip("`").split(".") if p.strip("`")]
                if len(parts) >= 2:
                    used.add(parts[-2].lower())
            illegal = used - allowed
            if illegal:
                raise ComputeError(f"filled bigquery query selects off-allowlist datasets: {sorted(illegal)}")
        return await bigquery_tools.bigquery_sql_execute({"query": query}, {"tenant_id": str(tenant_id), "db": db})

    # Default / "suiteql": NetSuite SuiteTalk REST. Expression-leaf metrics are
    # themselves single-source rows (suiteql) and route here.
    from app.core.config import settings
    from app.mcp.tools import netsuite_suiteql

    # Re-validate the FILLED query with the FULL allowlist check (read-only AND
    # table-allowlist), not just is_read_only_sql. Params have already been
    # type-coerced + filled, so this is the last gate before the number leaves the
    # metric layer: a blessed query that selects from an off-allowlist table is
    # read-only-clean yet table-illegal, and must NOT reach execute under the
    # catalog's authority. Build allowed_tables from settings (same source the tool
    # itself uses) so the metric layer and the tool agree on what is permitted.
    allowed_tables = {t.strip().lower() for t in settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")}
    try:
        netsuite_suiteql.validate_query(query, allowed_tables)
    except ValueError as ex:
        # A blessed query that fails the table-allowlist (or read-only) re-validation is
        # a spec/schema-drift condition: raise ComputeError so compute_metric audit-logs
        # the failure and returns a NUMBER-FREE error dict (compute is read-only) — never a
        # wrong number, never the off-allowlist result.
        raise ComputeError(f"filled query failed allowlist validation: {ex}") from ex
    return await netsuite_suiteql.execute({"query": query}, {"tenant_id": str(tenant_id), "db": db})


async def _execute_scalar_query(db, tenant_id, metric: MetricDefinition, coerced: dict, context: dict) -> float:
    query = fill_query(metric.blessed_spec["query"], coerced)
    # Branch on source_kind so the number comes from the RIGHT data source.
    result = await _validate_and_execute_by_source(db, tenant_id, metric.source_kind, query)
    # Fail closed: a failed query must NEVER be coerced into a fabricated 0.0.
    if isinstance(result, dict) and result.get("error"):
        raise ComputeError(str(result.get("message") or "blessed query failed"))
    rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ComputeError("blessed query returned no rows")
    first = rows[0]
    if not isinstance(first, (list, tuple)) or len(first) < 1:
        raise ComputeError("blessed query returned no columns")
    cell = first[0]
    if cell is None:
        raise ComputeError("blessed query returned a null value")
    try:
        return float(cell)
    except (TypeError, ValueError) as ex:
        # M1: never embed the cell value in the error message — it is a computed number
        # (e.g. "$1,234.00") that would leak through the error dict's 'message' key to
        # the LLM (error dicts are NOT suppressed metric payloads). Static message only.
        raise ComputeError("blessed query returned a non-numeric value") from ex


async def resolve_metric_by_key(db: AsyncSession, *, tenant_id, key: str) -> MetricDefinition | None:
    """Exact-key lookup with tenant-override-by-key semantics (tenant row wins over SYSTEM).

    Compute requests name a metric by its exact key, so this must NOT route through the
    embedding-similarity resolver: with seeded intent_embeddings a sibling metric whose
    embedding ranks nearer to the key string can evict the requested row out of a narrow
    top_k slice, yielding a false 'no_blessed_definition'/'missing_dependency'. A direct
    keyed query is independent of embeddings and catalog size.
    """
    stmt = select(MetricDefinition).where(
        or_(
            MetricDefinition.tenant_id == tenant_id,
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
        ),
        MetricDefinition.status == "active",
        MetricDefinition.key == key,
    )
    rows = list((await db.execute(stmt)).scalars().all())
    # Tenant override wins by key (mirrors resolve_metrics' by_key precedence).
    tenant_row = next((r for r in rows if r.tenant_id == tenant_id), None)
    return tenant_row or next((r for r in rows), None)


async def _log_compute_failure(
    db: AsyncSession, *, tenant_id, metric: MetricDefinition, error_code: str, message: str
) -> dict:
    """D1: compute is READ-ONLY. A failed/unusable blessed query is recorded to the
    audit log for observability but NEVER mutates the definition (no status flip — a
    SYSTEM row is shared across tenants and a tenant's schema-drift must not disable it
    for everyone, and a flush riding the chat turn is non-durable anyway). Returns a
    NUMBER-FREE structured error dict. Quarantine/reactivation is an admin/author action
    (Task 9), not a side effect of a read."""
    from app.services import audit_service

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="metric",
        action="metric.compute.failed",
        actor_type="system",
        resource_type="metric_definition",
        resource_id=str(metric.id),
        status="failed",
        error_message=message,
        # §10 audit-citation: include the definition version that was active when the
        # failure occurred, so the audit trail cites which version failed.
        payload={"key": metric.key, "error": error_code, "version": metric.version},
    )
    return {"error": error_code, "key": metric.key, "message": message}


async def compute_metric(db: AsyncSession, *, tenant_id, key: str, params: dict, context: dict) -> dict:
    metric = await resolve_metric_by_key(db, tenant_id=tenant_id, key=key)
    if metric is None:
        return {
            "error": "no_blessed_definition",
            "key": key,
            "message": f"No blessed definition for '{key}'.",
        }
    # R3#24 guard: reject a caller param that is in NEITHER params_schema NOR the metric's
    # declared dimensions, giving a precise "not a recognised param or dimension" error.
    # This runs AFTER the metric-not-found check (so an unknown KEY still returns
    # no_blessed_definition) and BEFORE coerce_params (so the error code is precise, not the
    # generic invalid_params that coerce would produce for the same unrecognised key).
    # Advisory note: a param that IS a declared dimension but NOT in params_schema will
    # still be rejected downstream by coerce_params ("unknown param: <name>") — there is no
    # free-dimension SQL binding yet, so a dimension-only param can't actually slice the
    # query. That is expected and acceptable; this guard's job is precision for params that
    # are unrecognised by BOTH schemas.
    allowed_dims = (
        set(metric.dimensions.keys()) if isinstance(metric.dimensions, dict) else set(metric.dimensions or [])
    )
    schema_keys = set(metric.params_schema or {})
    for p in params:
        if p not in schema_keys and p not in allowed_dims:
            return {
                "error": "invalid_dimension",
                "key": key,
                "message": f"'{p}' is not a declared param or dimension of '{key}'",
            }

    fy = int(context.get("fiscal_year_start_month", 1) or 1)
    # G1: param coercion is a CALLER-input gate, not a metric/schema-drift failure, so it
    # is handled separately from the ExpressionError/ComputeError path below. A bad param
    # (unknown/missing key, malformed date/enum) raises ParamError; a fabricated period
    # token not in period_resolver.SUPPORTED_TOKENS raises PeriodError. Both must yield the
    # §9 number-free structured refusal — NOT bare-raise out of compute_metric and 500 the
    # request (every other refusal path returns an {'error': ...} dict). This wraps BOTH the
    # top-level coerce and the per-leaf coerce in the expression path; refusal precedes any
    # execution (the executor is never reached) and does NOT flip the metric to needs_review
    # (the metric is fine — the caller's params were not).
    try:
        coerced = coerce_params(metric.params_schema or {}, params, fiscal_year_start_month=fy)
    except (ParamError, PeriodError) as ex:
        return {"error": "invalid_params", "key": key, "message": str(ex)}
    # The Period column label is the period token the caller passed. coerce_params resolves
    # a period-type param by its SCHEMA-DECLARED name (which need not be literally "period"),
    # so derive that name instead of hardcoding "period" — otherwise a metric that declares
    # its period under another name (e.g. "window") renders a blank Period column.
    _period_name = next(
        (n for n, s in (metric.params_schema or {}).items() if isinstance(s, dict) and s.get("type") == "period"),
        None,
    )
    period_label = params.get(_period_name, "") if _period_name else ""

    try:
        if metric.source_kind == "expression":
            leaves = {}
            for dep in metric.depends_on or []:
                dmatch = await resolve_metric_by_key(db, tenant_id=tenant_id, key=dep)
                if dmatch is None:
                    return {
                        "error": "missing_dependency",
                        "key": dep,
                        "message": f"Missing leaf metric '{dep}'.",
                    }
                # M3: a leaf that is itself an expression metric has blessed_spec=None;
                # calling _execute_scalar_query on it would crash (TypeError) when it
                # tries to dereference dmatch.blessed_spec["query"]. Author-time rejects
                # NEW nested-expression metrics, but a seeded/pre-existing/edited row can
                # still reach compute. Return a number-free structured error instead of
                # crashing — do NOT execute, do NOT mutate.
                if dmatch.source_kind == "expression":
                    return {
                        "error": "nested_expression_unsupported",
                        "key": dep,
                        "message": (
                            f"expression leaf '{dep}' is itself an expression; only query-backed leaves are supported"
                        ),
                    }
                try:
                    leaf_coerced = coerce_params(dmatch.params_schema or {}, params, fiscal_year_start_month=fy)
                except (ParamError, PeriodError) as ex:
                    return {"error": "invalid_params", "key": key, "message": str(ex)}
                leaves[dep] = await _execute_scalar_query(
                    db,
                    tenant_id,
                    dmatch,
                    leaf_coerced,
                    context,
                )
            value = evaluate_expression(metric.expression, leaves)
        else:
            value = await _execute_scalar_query(db, tenant_id, metric, coerced, context)
    except ParamError as ex:
        # fill_query raises ParamError ('unfilled placeholder remains') deep inside
        # _execute_scalar_query when the blessed query references a placeholder that
        # coerce_params never filled (e.g. a :token NOT declared in params_schema).
        # This is a caller/spec-param condition, NOT a metric/schema-drift failure, so it
        # mirrors the coerce gates above: return the §9 number-free structured refusal —
        # NOT bare-raise out of compute_metric and 500 the request, and NOT flip status.
        return {"error": "invalid_params", "key": key, "message": str(ex)}
    except ExpressionError as ex:
        # Runtime evaluator failure → no number. Div-by-zero is the canonical case
        # (missing-dep/cycle are author-time rejections); label it precisely.
        code = "division_by_zero" if "division by zero" in str(ex).lower() else "expression_failed"
        return await _log_compute_failure(db, tenant_id=tenant_id, metric=metric, error_code=code, message=str(ex))
    except ComputeError as ex:
        # Blessed query failed / returned an unusable result → no number.
        return await _log_compute_failure(
            db, tenant_id=tenant_id, metric=metric, error_code="blessed_query_failed", message=str(ex)
        )

    # F4 (c): label the data_table with the metric KEY (a string), never the
    # blessed_spec/expression — keep the internal execution spec OUT of the SSE payload.
    # §10: pass definition_version so the payload cites which definition produced the number.
    # M4: pass source_kind so the orchestrator's source-pin logic can distinguish BigQuery
    # from SuiteQL/NetSuite without re-querying the catalog.
    return metric_data_table(
        metric.display_name,
        value,
        metric.unit,
        period_label,
        metric.key,
        definition_version=metric.version,
        source_kind=metric.source_kind,
    )
