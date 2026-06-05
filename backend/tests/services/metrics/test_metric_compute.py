# backend/tests/services/metrics/test_metric_compute.py
import uuid

import pytest

from app.services.metrics.metric_compute import ParamError, coerce_params, fill_query


@pytest.mark.asyncio
async def test_bigquery_metric_offallowlist_dataset_refuses(db, monkeypatch):
    from app.core.config import settings
    from app.services.metrics import metric_compute

    monkeypatch.setattr(settings, "BIGQUERY_ALLOWED_DATASETS", "analytics", raising=False)
    with pytest.raises(metric_compute.ComputeError, match="allowlist"):
        await metric_compute._validate_and_execute_by_source(
            db, uuid.uuid4(), "bigquery", "SELECT x FROM secret_dataset.t"
        )


@pytest.mark.asyncio
async def test_bigquery_3part_ref_extracts_dataset_not_project(db, monkeypatch):
    """R1#7 hole 1 (CRITICAL): a fully-qualified `project.dataset.table` ref. The DATASET
    is the SECOND-to-last dotted component (`secret_dataset`), NOT the project (`proj`).
    The naive `FROM <id>\\.` regex captured the FIRST component (the project), so it
    over-blocked legit cross-project queries AND, worse, under-blocked the attack
    `FROM allowed.secret_dataset.events` (it captured `allowed` and never checked
    `secret_dataset`). The allowlist error must name `secret_dataset`, never `proj`."""
    from app.core.config import settings
    from app.services.metrics import metric_compute

    monkeypatch.setattr(settings, "BIGQUERY_ALLOWED_DATASETS", "analytics", raising=False)
    with pytest.raises(metric_compute.ComputeError, match="secret_dataset") as ei:
        await metric_compute._validate_and_execute_by_source(
            db, uuid.uuid4(), "bigquery", "SELECT x FROM proj.secret_dataset.t"
        )
    # The PROJECT id must NOT be reported as a dataset.
    assert "proj" not in str(ei.value), str(ei.value)


@pytest.mark.asyncio
async def test_bigquery_join_clause_is_enforced(db, monkeypatch):
    """R1#7 hole 2: JOIN clauses bypassed the check entirely (the regex only matched
    FROM). `FROM analytics.a JOIN secret.b` let `secret` straight through. JOIN-ed
    datasets must be enforced exactly like FROM-ed ones (mirror of SuiteQL parse_tables,
    which already does `(?:FROM|JOIN)`)."""
    from app.core.config import settings
    from app.services.metrics import metric_compute

    monkeypatch.setattr(settings, "BIGQUERY_ALLOWED_DATASETS", "analytics", raising=False)
    with pytest.raises(metric_compute.ComputeError, match="secret"):
        await metric_compute._validate_and_execute_by_source(
            db, uuid.uuid4(), "bigquery", "SELECT x FROM analytics.a JOIN secret.b ON a.id = b.id"
        )


@pytest.mark.asyncio
async def test_bigquery_allowlist_is_case_insensitive(db, monkeypatch):
    """R1#7 hole 3: lowercase `from`/`join` extracted nothing, so the allowlist was
    bypassed by simply lowercasing the keyword. Extraction must be case-insensitive
    (mirror of SuiteQL parse_tables' re.IGNORECASE)."""
    from app.core.config import settings
    from app.services.metrics import metric_compute

    monkeypatch.setattr(settings, "BIGQUERY_ALLOWED_DATASETS", "analytics", raising=False)
    with pytest.raises(metric_compute.ComputeError, match="secret"):
        await metric_compute._validate_and_execute_by_source(db, uuid.uuid4(), "bigquery", "select x from secret.t")


@pytest.mark.asyncio
async def test_bigquery_onallowlist_3part_passes_dataset_gate(db, monkeypatch):
    """A 3-part ref whose DATASET is on the allowlist (`proj.analytics.t`) must pass the
    dataset gate — the off-allowlist ComputeError must NOT be raised. Execution may still
    fail downstream (no real BQ connection in the test DB), so we only assert the SPECIFIC
    'off-allowlist' refusal is absent; any other failure is acceptable here."""
    from app.core.config import settings
    from app.services.metrics import metric_compute

    monkeypatch.setattr(settings, "BIGQUERY_ALLOWED_DATASETS", "analytics", raising=False)
    try:
        await metric_compute._validate_and_execute_by_source(
            db, uuid.uuid4(), "bigquery", "SELECT x FROM proj.analytics.t"
        )
    except metric_compute.ComputeError as ex:
        assert "off-allowlist" not in str(ex), str(ex)
    except Exception:
        # Any non-allowlist failure (e.g. no BQ connection) is fine — the gate let it through.
        pass


@pytest.mark.asyncio
async def test_bigquery_empty_allowlist_is_noop(db, monkeypatch):
    """Backward-compat: an EMPTY BIGQUERY_ALLOWED_DATASETS disables the check entirely.
    `FROM secret.t` must NOT raise the off-allowlist ComputeError (it may fail later for
    other reasons — no BQ connection — but never on the allowlist gate)."""
    from app.core.config import settings
    from app.services.metrics import metric_compute

    monkeypatch.setattr(settings, "BIGQUERY_ALLOWED_DATASETS", "", raising=False)
    try:
        await metric_compute._validate_and_execute_by_source(db, uuid.uuid4(), "bigquery", "SELECT x FROM secret.t")
    except metric_compute.ComputeError as ex:
        assert "off-allowlist" not in str(ex), str(ex)
    except Exception:
        pass


def test_coerce_rejects_unknown_param():
    with pytest.raises(ParamError):
        coerce_params({"period_start": {"type": "date"}}, {"evil": "1 OR 1=1"})


def test_coerce_enum_rejects_out_of_set():
    with pytest.raises(ParamError):
        coerce_params({"region": {"type": "enum", "values": ["us", "eu"]}}, {"region": "'; DROP"})


def test_coerce_int_and_date():
    out = coerce_params(
        {"sub": {"type": "int"}, "period_start": {"type": "date"}},
        {"sub": "7", "period_start": "2026-01-01"},
    )
    assert out == {"sub": 7, "period_start": "2026-01-01"}


def test_fill_query_uses_coerced_literals():
    sql = fill_query("SELECT x WHERE sub=:sub AND d>=:period_start", {"sub": 7, "period_start": "2026-01-01"})
    assert sql == "SELECT x WHERE sub=7 AND d>='2026-01-01'"


def test_fill_query_rejects_residual_placeholder():
    with pytest.raises(ParamError):
        fill_query("SELECT x WHERE sub=:sub", {})


def test_fill_query_escapes_embedded_single_quote():
    """REAL injection invariant (F3, leg a). fill_query renders a string-typed coerced
    value inside a SQL string literal (`'<v>'`). If `v` itself contains a single quote,
    the naive `f"'{v}'"` breaks OUT of the literal, turning param data into SQL control.
    The classic payload `x' OR '1'='1` must be rendered as a SINGLE, structurally-inert
    string literal — every embedded quote doubled (`''`) per SQL escaping. The prior
    code did `f"'{v}'"` with no escaping, so the rendered SQL was
    `'x' OR '1'='1'` — three literals + boolean logic, an injection break-out."""
    payload = "x' OR '1'='1"
    out = fill_query("SELECT x WHERE region=:region", {"region": payload})
    # The whole value is one inert literal: every embedded ' is doubled to ''.
    assert out == "SELECT x WHERE region='x'' OR ''1''=''1'"
    # And there is no un-doubled quote that could close the literal early: stripping
    # the doubled-quote pairs leaves exactly the two outer delimiters.
    assert out.replace("''", "").count("'") == 2


def test_fill_query_backslash_group_ref_does_not_inject_placeholder_text():
    """REAL injection invariant (F3, leg a — backslash gap). fill_query substitutes
    each `:name` via `re.sub(pattern, _render(val), query)`. The SECOND argument of
    re.sub is a REPLACEMENT TEMPLATE: re.sub interprets `\\g<0>`, `\\1`, `\\g<name>`
    in it. _render doubles single quotes but does NOT escape backslashes, and the
    author-time guard rejects `'`/`;`/`--` but NOT backslash — so a blessed enum value
    like `us\\g<0>` smuggles a group-reference into the template. `\\g<0>` re-expands to
    the WHOLE match (`:region`), injecting the placeholder text back into the SQL.

    Concretely: with the buggy template substitution the rendered query is
    `region='us:region'` — the value `us\\g<0>` did NOT land verbatim; `\\g<0>` was
    interpreted and replaced with the matched `:region` text INSIDE the literal. The
    value MUST land exactly as written (one inert literal): `region='us\\g<0>'`."""
    out = fill_query("SELECT x WHERE region=:region", {"region": "us\\g<0>"})
    # The value lands VERBATIM — the backslash group-ref was NOT interpreted by re.sub.
    assert out == "SELECT x WHERE region='us\\g<0>'", out
    # And the placeholder text `:region` was NOT re-injected into the SQL.
    assert ":region" not in out, out


def test_fill_query_backslash_numbered_group_ref_does_not_raise():
    """REAL fail-closed invariant (F3, leg a — backslash gap). A blessed enum value
    like `a\\1b` becomes the re.sub replacement template `'a\\1b'`. re.sub reads `\\1`
    as a reference to capture group 1 — which the placeholder pattern does NOT have —
    and raises `re.error: invalid group reference 1`. fill_query catches only nothing
    here, so the bare re.error propagates out; compute_metric catches only
    ExpressionError/ComputeError, so the request 500s instead of failing closed.

    fill_query MUST treat the value as data: render it verbatim as one inert literal,
    NEVER raise (and never inject substituted text). Pre-fix this raises re.error."""
    out = fill_query("SELECT x WHERE region=:region", {"region": "a\\1b"})
    assert out == "SELECT x WHERE region='a\\1b'", out
    # group-ref text never leaked: the value is the literal three chars a \ 1 b.
    assert out.endswith("'a\\1b'"), out


def test_fill_query_lone_backslash_renders_verbatim():
    """A trailing/lone backslash in a blessed value must also land verbatim inside the
    literal — not be consumed or escaped by the substitution machinery."""
    out = fill_query("SELECT x WHERE region=:region", {"region": "eu\\"})
    assert out == "SELECT x WHERE region='eu\\'", out
