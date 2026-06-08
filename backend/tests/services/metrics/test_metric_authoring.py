# backend/tests/services/metrics/test_metric_authoring.py
import pytest

from app.services.metrics.metric_authoring import AuthoringError, validate_definition

_CROSS_SOURCE_KEYS = {
    "left_query",
    "left_dialect",
    "right_query",
    "right_dialect",
    "join_keys",
    "join_type",
    "select",
    "pivot",
}


def test_rejects_unknown_cross_source_key():
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "x",
                "source_kind": "cross_source",
                "blessed_spec": {"left_query": "a", "right_query": "b", "join_keys": ["id"], "aggregations": "sum"},
            },
            allowed_cross_source_keys=_CROSS_SOURCE_KEYS,
        )


def test_rejects_expression_cycle():
    with pytest.raises(AuthoringError):
        validate_definition({"key": "a", "source_kind": "expression", "expression": "a / b", "depends_on": ["a", "b"]})


def test_rejects_both_spec_and_expression():
    with pytest.raises(AuthoringError):
        validate_definition(
            {"key": "x", "source_kind": "suiteql", "blessed_spec": {"query": "SELECT 1"}, "expression": "a/b"}
        )


def test_accepts_valid_expression():
    validate_definition(
        {
            "key": "net_margin",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        }
    )


def test_rejects_expression_with_disallowed_operator():
    """REAL author-time invariant (T2 multi-angle gate, MAJOR). validate_definition for
    expression metrics checked the dependency NAMES (extract_dependencies) but never the
    OPERATOR allowlist that evaluate_expression enforces at compute time (+ - * / plus
    unary minus / numeric constants / names). So an expression using **, %, //, or a
    comparison passed validation and persisted status='active', then raised
    ExpressionError('disallowed token: ...') on EVERY compute — a blessed, 'active' metric
    that can never produce a number, defeating the catalog's author-time computability
    guarantee. validate_definition MUST reject any expression whose nodes/operators
    evaluate_expression cannot evaluate.

    Pre-fix this PASSES (only deps were checked); post-fix it raises AuthoringError."""
    for bad_expr in [
        "net_income ** gross_revenue",  # power
        "net_income % gross_revenue",  # modulo
        "net_income // gross_revenue",  # floor-div
    ]:
        with pytest.raises(AuthoringError):
            validate_definition(
                {
                    "key": "net_margin_v2",
                    "source_kind": "expression",
                    "expression": bad_expr,
                    "depends_on": ["net_income", "gross_revenue"],
                }
            )


def test_accepts_expression_with_allowed_operators_only():
    """Guard against over-rejection: the operator allowlist must still accept the four
    blessed arithmetic ops (+ - * /), grouping parens, unary minus, and numeric constants
    — so a legitimate expression like '(a - b) / 2' is not falsely rejected."""
    validate_definition(
        {
            "key": "delta_per_half",
            "source_kind": "expression",
            "expression": "(net_income - gross_revenue) / 2",
            "depends_on": ["net_income", "gross_revenue"],
        }
    )


# ── (b) params_schema type allowlist + :param binding ──────────────────────────


def test_rejects_param_type_not_in_allowlist():
    """REAL invariant: a free-text 'string' param type lets unconstrained text flow
    into the filled SQL (the §6 binding hole). validate_definition MUST reject any
    param type outside {date,int,enum,period}. The prior code never inspected
    params_schema at all, so this string param sailed through to compute."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "gross_revenue",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE name=:name", "dialect": "suiteql"},
                "params_schema": {"name": {"type": "string"}},
            }
        )


def test_rejects_enum_without_values():
    """An enum param with no (or empty) values list is an open hole — coerce_params
    would reject every value at runtime, but author-time must catch the malformed
    declaration so a blessed metric is never persisted un-runnable."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "rev_by_region",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE region=:region", "dialect": "suiteql"},
                "params_schema": {"region": {"type": "enum", "values": []}},
            }
        )


def test_rejects_query_placeholder_not_declared_in_params_schema():
    """Every :name in the blessed query MUST be declared in params_schema, else
    fill_query would leave a residual placeholder (or worse, an undeclared param
    bypasses type coercion). The prior code never cross-checked the two."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "gross_revenue",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE sub=:subsidiary", "dialect": "suiteql"},
                "params_schema": {"period": {"type": "period"}},  # :subsidiary undeclared
            }
        )


def test_rejects_declared_param_absent_from_query():
    """And vice-versa: a declared non-period param that never appears as a :name in
    the query is dead config that silently never binds — reject it at author-time.
    (period is exempt: it expands to :period_start/:period_end, not a literal :period.)"""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "gross_revenue",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE d>=:period_start AND d<=:period_end", "dialect": "suiteql"},
                "params_schema": {
                    "period": {"type": "period"},
                    "subsidiary": {"type": "int"},  # declared but never referenced
                },
            }
        )


def test_accepts_period_expanding_to_start_end_placeholders():
    """A period param legitimately drives :period_start/:period_end placeholders
    (coerce_params expands it). This well-formed query-backed metric must PASS —
    guards against the binding check being too strict and rejecting valid period use."""
    validate_definition(
        {
            "key": "gross_revenue",
            "source_kind": "suiteql",
            "blessed_spec": {
                "query": "SELECT SUM(amount) FROM transactionline WHERE trandate>=:period_start AND trandate<=:period_end",
                "dialect": "suiteql",
            },
            "params_schema": {"period": {"type": "period"}},
        }
    )


def test_accepts_int_and_enum_params_bound_in_query():
    """Well-formed int + enum params, each referenced as a :name in the query and
    each carrying a valid type (enum with non-empty values) → PASS."""
    validate_definition(
        {
            "key": "rev_by_sub_region",
            "source_kind": "suiteql",
            "blessed_spec": {
                "query": "SELECT SUM(amount) FROM transactionline WHERE subsidiary=:sub AND region=:region",
                "dialect": "suiteql",
            },
            "params_schema": {
                "sub": {"type": "int"},
                "region": {"type": "enum", "values": ["us", "eu"]},
            },
        }
    )


# ── (b) seeder ↔ validate_definition consistency ───────────────────────────────


def test_period_param_is_optionally_binding():
    """REAL seeder-consistency invariant (F4 (b)). A `period` param is server-resolved
    into :period_start/:period_end; it is benign — no untrusted text flows into SQL.
    A blessed query that declares `period` but does NOT yet reference either bound
    (e.g. a stub `SELECT 0`, or a query that does not slice by date) must be ACCEPTED:
    period is OPTIONALLY-binding, unlike date/int/enum params (which must bind a
    placeholder). The prior strict rule rejected this as 'declared but the query binds
    neither :period_start nor :period_end', which is exactly what the seeded defaults
    trip on. Pre-fix this RAISES AuthoringError; post-fix it passes."""
    validate_definition(
        {
            "key": "cash",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "params_schema": {"period": {"type": "period"}},
        }
    )


def test_non_period_param_still_must_bind():
    """Guard: relaxing period to optionally-binding must NOT relax the binding rule for
    date/int/enum. A declared `int` param that the query never references is still dead
    config and must be rejected — proving the relaxation is period-specific."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "x",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
                "params_schema": {"sub": {"type": "int"}},  # declared, never bound → reject
            }
        )


def test_validate_definition_accepts_every_seeded_metric_payload():
    """REAL seeder-consistency invariant (F4 (b)). The system seeder ships
    params_schema={'period': {'type': 'period'}} for every metric, while the suiteql
    stubs carry `SELECT 0` (no :period_* placeholder). validate_definition MUST accept
    the EXACT payload the seeder persists for EVERY seeded metric — author-time
    validation and the seeder cannot disagree, else the blessed defaults are
    un-authorable. We reconstruct each seeded row's validation-relevant payload verbatim
    from _SYSTEM_METRICS and assert validate_definition accepts all 9.

    Pre-fix the 6 suiteql metrics RAISE (period declared, neither bound); post-fix all 9
    pass."""
    from app.services.metrics.metric_catalog_seeder import _SYSTEM_METRICS

    for m in _SYSTEM_METRICS:
        sk = m["source_kind"]
        payload = {
            "key": m["key"],
            "source_kind": sk,
            # Exactly what metric_catalog_seeder.seed_system_metrics writes.
            "blessed_spec": ({"query": "SELECT 0", "dialect": "suiteql"} if sk == "suiteql" else None),
            "expression": m.get("expression"),
            "depends_on": m.get("depends_on"),
            "params_schema": {"period": {"type": "period"}},
        }
        # Must not raise for ANY seeded metric.
        validate_definition(payload)


# ── F3 injection-hardening: reject injecty blessed enum values at author-time ───


def test_rejects_enum_value_with_sql_injection_payload():
    """REAL injection invariant (F3). An enum's `values` list is the catalog's set of
    BLESSED, author-trusted values that flow into the filled SQL at compute time
    (coerce_params accepts them verbatim; fill_query renders them inside a string
    literal). A blessed value that itself carries a single quote — the classic
    `x' OR '1'='1` payload — would, even after the fill_query quote-escape, smuggle
    SQL-control characters into the metric layer's trust surface. validate_definition
    MUST reject any enum value containing a single quote, ';', or '--' at author-time
    so such a value is NEVER persisted into the catalog. The prior code accepted any
    string in `values`, so this payload sailed through to a blessed metric row."""
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "rev_by_region",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 1 WHERE region=:region", "dialect": "suiteql"},
                "params_schema": {"region": {"type": "enum", "values": ["us", "x' OR '1'='1"]}},
            }
        )


def test_rejects_enum_value_with_sql_comment_or_semicolon():
    """Defense-in-depth: a blessed enum value carrying a statement terminator (';') or
    a SQL line-comment ('--') is equally an injection vector and must be rejected at
    author-time, even though neither is a quote."""
    for poison in ["us; DROP TABLE transactionline", "us-- comment"]:
        with pytest.raises(AuthoringError):
            validate_definition(
                {
                    "key": "rev_by_region",
                    "source_kind": "suiteql",
                    "blessed_spec": {"query": "SELECT 1 WHERE region=:region", "dialect": "suiteql"},
                    "params_schema": {"region": {"type": "enum", "values": ["eu", poison]}},
                }
            )


def test_rejects_enum_value_with_backslash():
    """REAL injection invariant (F3, leg b — backslash gap). fill_query substitutes
    :name via re.sub, whose REPLACEMENT TEMPLATE interprets backslash sequences
    (`\\g<0>`, `\\1`, `\\g<name>`). A blessed enum value carrying a backslash —
    e.g. `us\\g<0>` (re-injects the matched placeholder text) or `a\\1b` (raises an
    uncaught re.error → request 500s instead of failing closed) — is therefore an
    injection / fail-closed-breaking vector, on top of `'`/`;`/`--`. The compute-path
    callable-replacement fix makes the substitution backslash-inert at runtime, but the
    author-time guard MUST ALSO reject a backslash so such a value can NEVER be persisted
    into a blessed metric row (the commit's stated un-alterability invariant). The prior
    guard rejected `'`/`;`/`--` but NOT backslash, so these payloads sailed through."""
    for poison in ["us\\g<0>", "a\\1b", "eu\\"]:
        with pytest.raises(AuthoringError):
            validate_definition(
                {
                    "key": "rev_by_region",
                    "source_kind": "suiteql",
                    "blessed_spec": {"query": "SELECT 1 WHERE region=:region", "dialect": "suiteql"},
                    "params_schema": {"region": {"type": "enum", "values": ["eu", poison]}},
                }
            )


# ── (d) dialect vs source_kind cross-check ────────────────────────────────────


def test_dialect_must_match_source_kind():
    """REAL invariant (R1#12). The blessed_spec `dialect` field is author-supplied but
    compute routes entirely by source_kind — dialect is effectively a doc annotation.
    However, a contradictory dialect (e.g. source_kind=bigquery + dialect=suiteql)
    signals a copy-paste error and would silently compute via BigQuery while the metric
    claims to be SuiteQL. validate_definition MUST reject this mismatch at author-time
    so a blessed definition is internally consistent. A missing dialect (None) is
    allowed (authors need not always set it), and a matching dialect passes."""
    with pytest.raises(AuthoringError, match="dialect"):
        validate_definition(
            {
                "key": "k",
                "source_kind": "bigquery",
                "params_schema": {},
                "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            }
        )


def test_dialect_mismatch_suiteql_bigquery_also_rejected():
    """Mirror case: source_kind=suiteql with dialect=bigquery is equally contradictory
    and must be rejected. Proves the guard is symmetric."""
    with pytest.raises(AuthoringError, match="dialect"):
        validate_definition(
            {
                "key": "k",
                "source_kind": "suiteql",
                "params_schema": {},
                "blessed_spec": {"query": "SELECT 0", "dialect": "bigquery"},
            }
        )


def test_dialect_absent_is_allowed():
    """A missing dialect (key absent) is valid — authors need not always set it.
    Ensures the guard does not force a dialect declaration."""
    validate_definition(
        {
            "key": "cash",
            "source_kind": "suiteql",
            "params_schema": {"period": {"type": "period"}},
            "blessed_spec": {"query": "SELECT 0"},
        }
    )


def test_matching_dialect_passes():
    """source_kind=suiteql + dialect=suiteql is consistent and must pass."""
    validate_definition(
        {
            "key": "cash",
            "source_kind": "suiteql",
            "params_schema": {"period": {"type": "period"}},
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        }
    )


def test_bigquery_matching_dialect_passes():
    """source_kind=bigquery + dialect=bigquery is consistent and must pass."""
    validate_definition(
        {
            "key": "bq_metric",
            "source_kind": "bigquery",
            "params_schema": {"period": {"type": "period"}},
            "blessed_spec": {"query": "SELECT 0", "dialect": "bigquery"},
        }
    )


# ── (c) expression-over-expression DB check ────────────────────────────────────


@pytest.mark.asyncio
async def test_expression_leaf_must_be_query_backed(db):
    """REAL compute-safety invariant (R1#4). validate_leaves_exist currently only checks
    that the leaf keys EXIST and are active — it does not check that those leaves are
    query-backed. An expression whose leaf is also an expression is accepted at author
    time, then crashes at compute: _execute_scalar_query does
    `metric.blessed_spec["query"]`, and an expression leaf has `blessed_spec=None` →
    TypeError → 500. validate_leaves_exist MUST reject expression leaves at author time.

    Pre-fix this PASSES (currently any active key satisfies the check); post-fix it
    raises AuthoringError with 'query-backed' in the message."""
    import uuid

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.services.metrics.metric_authoring import AuthoringError, validate_leaves_exist

    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="leaf_expr",
            display_name="x",
            definition="x",
            unit="ratio",
            source_kind="expression",
            expression="a/b",
            depends_on=["a", "b"],
            status="active",
            version=1,
            provenance={"author": "t"},
        )
    )
    await db.flush()
    with pytest.raises(AuthoringError, match="query-backed"):
        await validate_leaves_exist(
            db,
            tenant_id=uuid.uuid4(),
            d={"source_kind": "expression", "depends_on": ["leaf_expr"], "key": "top"},
        )


# ── R1#9: 1536-d embedding guard on create + update authoring paths ──────────


@pytest.mark.asyncio
async def test_create_metric_rejects_non_1536_embedding(db, monkeypatch):
    """REAL dimension invariant (R1#9). The system seeder asserts len(vec)==1536 before
    persisting an intent embedding, but create_metric inserts whatever embed_domain_query
    returns with no guard. A wrong-dimension vector (e.g. 10-element list from a
    mis-configured provider) silently persists and corrupts cosine-similarity resolution.
    create_metric MUST raise AuthoringError mentioning '1536' when embed_domain_query
    returns a non-1536 non-None vector.

    Pre-fix this PASSES (no guard); post-fix raises AuthoringError matching '1536'."""
    import uuid

    from app.services.metrics import metric_authoring
    from app.services.metrics.metric_authoring import AuthoringError, create_metric

    async def _bad_embed(_text):
        return [0.0] * 10

    monkeypatch.setattr(metric_authoring, "embed_domain_query", _bad_embed)
    with pytest.raises(AuthoringError, match="1536"):
        await create_metric(
            db,
            tenant_id=uuid.uuid4(),
            payload={
                "key": "k1536",
                "display_name": "X",
                "definition": "x",
                "unit": "currency",
                "source_kind": "suiteql",
                "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
                "params_schema": {"period": {"type": "period"}},
            },
        )


@pytest.mark.asyncio
async def test_update_metric_rejects_non_1536_embedding(db, monkeypatch):
    """REAL dimension invariant (R1#9) — update path. update_metric recomputes the
    intent embedding when text fields change; if embed_domain_query returns a
    wrong-dimension vector, update_metric must raise AuthoringError mentioning '1536'
    rather than silently persisting a corrupted vector.

    Pre-fix this PASSES (no guard on update); post-fix raises AuthoringError '1536'."""
    import uuid

    # Seed a valid metric under SYSTEM_TENANT_ID so the FK constraint is satisfied
    # (create_metric calls ensure_system_tenant for SYSTEM rows). embed returns None
    # in the test env — that is explicitly allowed; only a non-None wrong-dim is rejected.
    from app.models.metric_definition import SYSTEM_TENANT_ID
    from app.services.metrics import metric_authoring
    from app.services.metrics.metric_authoring import AuthoringError, create_metric, update_metric

    metric = await create_metric(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        payload={
            "key": f"upd_1536_{uuid.uuid4().hex[:8]}",
            "display_name": "Original Name",
            "definition": "original definition",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "params_schema": {"period": {"type": "period"}},
        },
    )
    await db.flush()

    # Now monkeypatch to a bad-dimension embed and attempt a text-field update
    async def _bad_embed(_text):
        return [0.0] * 10

    monkeypatch.setattr(metric_authoring, "embed_domain_query", _bad_embed)
    with pytest.raises(AuthoringError, match="1536"):
        await update_metric(
            db,
            tenant_id=SYSTEM_TENANT_ID,
            metric_id=metric.id,
            payload={"display_name": "New Name"},
        )


# ── T2 gate (minor): normalize author synonyms to lowercase on write ──────────


@pytest.mark.asyncio
async def test_create_metric_normalizes_synonyms_to_lowercase(db, tenant_a):
    """REAL resolution invariant (T2 multi-angle gate, minor). The resolver's authoritative
    keyword branch matches synonyms via `synonyms.any(query.lower())` — exact, lowercased,
    full-phrase equality. create_metric persisted synonyms VERBATIM, so an author-typed
    mixed-case synonym ('AOV', 'Net Sales') could never match the lowercased query and
    silently degraded to vector-only resolution (the seeder already stores lowercase
    synonyms). create_metric MUST normalize synonyms to lowercase on write (also stripping
    blanks and deduping, order-preserving).

    Pre-fix synonyms persist verbatim; post-fix they are lowercased/stripped/deduped."""
    import uuid as _uuid

    from app.services.metrics.metric_authoring import create_metric

    metric = await create_metric(
        db,
        tenant_id=tenant_a.id,
        payload={
            "key": f"aov_{_uuid.uuid4().hex[:8]}",
            "display_name": "Average Order Value",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "params_schema": {"period": {"type": "period"}},
            "synonyms": ["AOV", "Net Sales", "  aov  "],  # mixed case + whitespace duplicate
        },
    )
    assert metric.synonyms == ["aov", "net sales"]


@pytest.mark.asyncio
async def test_update_metric_normalizes_synonyms_to_lowercase(db, tenant_a):
    """Update-path mirror: an edit that sets synonyms must normalize to lowercase too,
    else a mixed-case synonym patched via PUT silently never matches the lowercased query."""
    import uuid as _uuid

    from app.services.metrics.metric_authoring import create_metric, update_metric

    metric = await create_metric(
        db,
        tenant_id=tenant_a.id,
        payload={
            "key": f"upd_syn_{_uuid.uuid4().hex[:8]}",
            "display_name": "X",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "params_schema": {"period": {"type": "period"}},
            "synonyms": ["foo"],
        },
    )
    await db.flush()
    updated = await update_metric(
        db,
        tenant_id=tenant_a.id,
        metric_id=metric.id,
        payload={"synonyms": ["AOV", "Gross Profit"]},
    )
    assert updated.synonyms == ["aov", "gross profit"]
