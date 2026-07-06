from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.schemas.chart import ChartData
from app.schemas.report import DividerSection, HeadingSection, normalize_and_validate_sections
from app.services import audit_service
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_html import fmt_amount, render_report_html

_PLACEHOLDER = re.compile(r"\{\{(result|metric):([^}]+)\}\}")
_METRIC_COLUMNS = ["Metric", "Value", "Unit", "Period"]
# Cap chart points: unlike the table branch, the chart branch emits 2+ SVG nodes per row
# per series, so a 50k-row payload bakes a multi-MB SVG into the JSONB spec + rendered_html
# (the DoS-shape the table cap guards against). A chart is also illegible past a few dozen
# categories. Refuse deterministically and tell the model to aggregate first, rather than
# silently truncating (a truncated chart misrepresents the data with no signal).
_MAX_CHART_POINTS = 100
# A report presents the first K rows ("top numbers only"), never the raw detail dump.
# Product intent (2026-06-30): every report — financial AND data-analytics — is a summary
# + chart, not a long table. Curation keeps SOURCE ORDER (a statement's line sequence /
# a query's ORDER BY) rather than re-ranking, which would scramble ordered statements and
# mis-rank when the value column can't be identified. The model will not reliably curate
# from prompt guidance alone (proven live), so the resolver enforces it.
_REPORT_TABLE_TOP_K = 12
# A table needs at least this many rows to be worth auto-charting.
_MIN_AUTO_CHART_ROWS = 2
# Statement treatment (product shape confirmed 2026-07-01: "callouts + statement"): a
# statement-shaped result (line_meta present — flattened ns_runReport statements) renders
# as up to _STATEMENT_CALLOUT_MAX marquee metric_headline cards + a curated table of at
# most _STATEMENT_TABLE_MAX NAMED section-summary lines. Selection is purely STRUCTURAL
# (line_meta.is_summary/level + a non-null amount) — never label matching, so blanks and
# amount-less placeholder rows drop out without any hardcoded names (no prompt
# pollution). Fewer than _MIN_STATEMENT_LINES qualifying lines ⇒ not a statement ⇒ the
# general top-K floor applies (all non-statement reports keep today's behavior).
_STATEMENT_TABLE_MAX = 8
_STATEMENT_CALLOUT_MAX = 4
_MIN_STATEMENT_LINES = 2
# On a REAL NetSuite statement (section rows tagged via line_meta.is_section), the shallowest
# section level with fewer than this many lines is enriched with the next level's subtotals —
# so a P&L (only 3 net lines at the top level) shows Revenue/COGS/Gross Profit, while a cash
# flow (7 top-level flows) stays at its top level and does not pull in sub-section noise.
_STATEMENT_MIN_SECTIONS = 4
# Time-series detection (Phase 4): a chart whose x column is a strictly ORDERED period
# sequence renders as a LINE (a trend), not account-style bars. Matched on the VALUES'
# shape only — never on column names (no schema assumptions). Conservative: any miss
# keeps the bar default, which is always safe.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
# A period label's month token must be a real month WORD, not merely a 3-letter prefix of
# some other word ("Marketing"->"mar", "Junior"->"jun", "Decoy"->"dec"). Match the whole
# captured word against the recognized abbreviations + full names (T2 gate: minor — a
# month-prefixed category would otherwise render as a false trend line).
_MONTH_WORDS = set(_MONTHS) | {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december", "sept",
}  # fmt: skip
_ISO_RE = re.compile(r"^((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])(?:[-/](\d{1,2}))?$")  # 2026-06(-30)
_MYYYY_RE = re.compile(r"^(0?[1-9]|1[0-2])/((?:19|20)\d{2})$")  # 06/2026
_MONYYYY_RE = re.compile(r"^([a-z]{3,9})[ .,'-]*(?:((?:19|20)\d{2})|'(\d{2}))$")  # Jun 2026 / Jun '26
_QY_RE = re.compile(r"^q([1-4])[ .'-]?(?:((?:19|20)\d{2})|(\d{2}))$")  # Q2 2026 / Q2 26
_YQ_RE = re.compile(r"^((?:19|20)\d{2})[ .-]?q([1-4])$")  # 2026 Q2
_FY_RE = re.compile(r"^fy[ .'-]?(?:((?:19|20)\d{2})|(\d{2}))$")  # FY26 / FY2026
Resolver = Callable[[str], dict]


def _period_key(value: str) -> tuple | None:
    """Parse a period/date-shaped label to a sortable ``(year, sub, day)`` key, or None
    when the value is not a plausible period. Plausible-year required everywhere (19xx/
    20xx) so decimals ("2026.5", "1.2500"), codes ("5-2028"), implausible years
    ("9999-01"), and month-word+count labels ("May 100") never register as periods."""
    s = str(value).strip().lower()
    m = _ISO_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    m = _MYYYY_RE.match(s)
    if m:
        return (int(m.group(2)), int(m.group(1)), 0)
    m = _MONYYYY_RE.match(s)
    if m and m.group(1) in _MONTH_WORDS:
        year = int(m.group(2)) if m.group(2) else 2000 + int(m.group(3))
        return (year, _MONTHS[m.group(1)[:3]], 0)
    m = _QY_RE.match(s)
    if m:
        year = int(m.group(2)) if m.group(2) else 2000 + int(m.group(3))
        return (year, int(m.group(1)) * 3, 0)
    m = _YQ_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)) * 3, 0)
    m = _FY_RE.match(s)
    if m:
        year = int(m.group(1)) if m.group(1) else 2000 + int(m.group(2))
        return (year, 0, 0)
    return None


def _period_direction(x_values: list) -> str | None:
    """``"asc"``/``"desc"`` when EVERY x value parses as a period (``_period_key``) AND the
    sequence is STRICTLY MONOTONIC; else ``None``. Underlies ``_looks_time_series`` and lets
    the chart layer orient a line chronologically — a DESC "last N months" series is
    legitimately ordered newest-first but must still be PLOTTED oldest→newest.

    - A None/blank x DISQUALIFIES outright: SQL rollup rows (GROUP BY ROLLUP /
      UNION ALL totals) emit NULL/blank for the period column and are still PLOTTED —
      excluding them from the check would bake a fabricated final spike into the trend.
    - An UNORDERED or duplicate-period sequence (GROUP BY without ORDER BY; UNION
      across subsidiaries) DISQUALIFIES: a row-order polyline over it draws a zigzag
      trajectory that does not exist (T2 gate: major). The old bar default was
      order-agnostic; a line must EARN its ordering."""
    if len(x_values) < _MIN_AUTO_CHART_ROWS:
        return None
    keys = []
    for v in x_values:
        if v is None or not str(v).strip():
            return None  # a NULL/blank x row is plotted too — never call this a trend
        key = _period_key(v)
        if key is None:
            return None
        keys.append(key)
    if all(a < b for a, b in zip(keys, keys[1:])):
        return "asc"
    if all(a > b for a, b in zip(keys, keys[1:])):
        return "desc"
    return None


def _looks_time_series(x_values: list) -> bool:
    """True when the x column is a strictly-monotonic period sequence (see
    ``_period_direction``) — the shape that renders as a LINE trend, not order-agnostic
    bars. A miss keeps the bar default, which is always safe."""
    return _period_direction(x_values) is not None


def _currency_in(payload: dict, cols: list) -> list:
    """The producer's currency-column tags narrowed to the columns present."""
    return [c for c in (payload.get("currency_columns") or []) if c in cols]


def _has_summary_lines(line_meta, rows: list, cols: list, currency_columns: list) -> bool:
    """True when ``line_meta`` is row-aligned AND at least one summary line CARRIES AN
    AMOUNT — the single definition of "statement-shaped with hierarchy" every
    honesty/soup gate keys off (table branch, explicit-chart branch).

    Amount-aware deliberately (T2 gate r5: major): the double-count hazard the
    statement gates exist for is a summary BAR beside its own detail bars — an
    amount-less summary (a bare header row like "Transaction Detail") can never become
    a bar, so it must NOT flip an ordinary detail LISTING into statement treatment
    (which would kill its guaranteed auto-chart and driver-substitute its explicit
    charts)."""
    if not (isinstance(line_meta, list) and len(line_meta) == len(rows)):
        return False
    amount_idx = _amount_index(cols, currency_columns)
    for row, meta in zip(rows, line_meta):
        if not isinstance(meta, dict) or not meta.get("is_summary"):
            continue
        amount = row[amount_idx] if amount_idx < len(row) else None
        if amount is None or (isinstance(amount, str) and not amount.strip()):
            continue
        return True
    return False


def _driver_title(n: int, cols: list, currency_columns: list) -> str:
    """The driver chart's title, named after the SAME column the drivers were ranked
    by (``_amount_index``) — one derivation for every driver-chart site."""
    idx = _amount_index(cols, currency_columns)
    value_name = cols[idx] if idx < len(cols) else "value"
    return f"Top {n} drivers by {value_name}"


def _amount_index(cols: list, currency_columns: list) -> int:
    """Index of the amount column: the producer-tagged currency column, else col 1."""
    if currency_columns and currency_columns[0] in cols:
        return cols.index(currency_columns[0])
    return 1


def _metric_fields(payload: dict) -> dict | None:
    """Detect the blessed-metric data_table shape and return its flattened fields.

    A blessed metric (``metric_compute.metric_data_table`` → ``extract_result_payload``
    Path 1) is a single-row table ``columns=['Metric','Value','Unit','Period']`` with
    NO top-level value/unit/period — the number lives in ``rows[0]``. This lifts the
    row back to the {label,value,unit,period,definition_version} the report headline +
    ``{{metric:id}}`` placeholders expect. Returns None for any non-metric payload so
    the caller can fall back to top-level reads (the hand-rolled unit-test stub shape).
    """
    if not isinstance(payload, dict):
        return None
    cols = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], (list, tuple)):
        return None
    is_metric_cols = cols == _METRIC_COLUMNS
    # source_kind is the metric trust-boundary marker (set by extract_result_payload
    # only for suppress_llm_value metric payloads) — accept it as a secondary signal.
    if not is_metric_cols and "source_kind" not in payload:
        return None
    row = rows[0]
    if len(row) < 4:
        return None
    return {
        "label": row[0],
        "value": row[1],
        "unit": row[2],
        "period": row[3],
        "definition_version": payload.get("definition_version"),
    }


def _coerce_number(value) -> float | None:
    """Return value as a FINITE float, stripping $, %, and thousands commas, or None if
    non-numeric / non-finite. Used to decide which table columns are chartable y-axes —
    NaN/Inf (incl. the string literals float() accepts) must NOT qualify a column or bake
    a nan/inf bar into a chart."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("$", "").replace("%", "").replace(",", "")
    if not cleaned:
        return None
    try:
        n = float(cleaned)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _numeric_value_columns(cols: list, rows: list) -> list:
    """The chartable y-axis columns: every column after col 0 (the x-axis) with at least
    one finite-numeric cell. Probes ALL rows (chart inputs are already bounded by
    _MAX_CHART_POINTS / curation), so a column that is null in the first rows but numeric
    later still qualifies. Column-wide, not row[0]."""
    return [
        c for i, c in enumerate(cols[1:], start=1) if any(i < len(r) and _coerce_number(r[i]) is not None for r in rows)
    ]


def _curate_table_rows(rows: list, k: int) -> tuple[list, bool]:
    """Curate a report table to the first K rows, PRESERVING the result's own order.

    Returns ``(rows, curated)``; a result at or under K is returned unchanged. We keep
    source order rather than re-rank by magnitude: a result is already meaningfully
    ordered (a statement's line sequence, or a query's ORDER BY), and re-ranking would
    both destroy that structure and mis-rank when the "value" column can't be identified.
    The narrative + chart carry the analysis; this just bounds the table to top numbers.
    """
    if len(rows) <= k:
        return rows, False
    return rows[:k], True


def _trim_statement_by_indent(picked: list) -> list:
    """Legacy trim (synthetic/indented ``line_meta``): keep the shallowest indent levels that
    fit ``_STATEMENT_TABLE_MAX`` and still contain the closing conclusion; else HEAD+TAIL.
    Behavior unchanged — the section-aware path runs only on real NetSuite data.

    ``picked`` is ``(level, index, row, amount)``. The lower bound matters: a lone shallow
    grand-total is a "fitting" subset of ONE that would collapse the statement to a single
    row (T2 gate r3). A subset also must CONTAIN the last qualifying line — the positional
    conclusion (Net Change / Ending Cash) — or the marquee figures are cut (T2 gate r5)."""
    if len(picked) <= _STATEMENT_TABLE_MAX:
        return picked
    for threshold in sorted({p[0] for p in picked}, reverse=True):
        subset = [p for p in picked if p[0] <= threshold]
        if _MIN_STATEMENT_LINES <= len(subset) <= _STATEMENT_TABLE_MAX and subset[-1] is picked[-1]:
            return subset
    head = _STATEMENT_TABLE_MAX - _STATEMENT_CALLOUT_MAX
    return picked[:head] + picked[-_STATEMENT_CALLOUT_MAX:]


def _same_section(row_a: list, row_b: list) -> bool:
    """True when one row's label is a suffix of the other's — a NetSuite subtotal's label is
    its section name with a prefix ("Operating Activities" ⊂ "Total Operating Activities"), so
    an ADJACENT header/subtotal pair (no detail rows between to bracket them) can still be
    recognized without relying on amount alone. Locale-degrading: a non-nesting label pair is
    simply not folded (both rows survive)."""
    a = str(row_a[0]).strip().lower() if row_a and row_a[0] is not None else ""
    b = str(row_b[0]).strip().lower() if row_b and row_b[0] is not None else ""
    if not a or not b or a == b:
        return False
    lo, hi = (a, b) if len(a) < len(b) else (b, a)
    return hi.endswith(lo)


def _select_statement_sections(picked: list) -> list:
    """Select the coherent lines from a REAL NetSuite statement's section rows.

    ``picked`` is ``(level, index, row, amount)`` in statement order.

    Step 1 — fold each section HEADER into its own subtotal. The two share a level and the
    SAME coerced NON-ZERO amount, the subtotal is the first later section at level <= the
    header's (and is NOT the closing conclusion), and the two labels NEST (``_same_section``:
    a subtotal's label ends with its section name, "Operating Activities" ⊂ "Total Operating
    Activities"). The label-nest check is what makes this SAFE — two DISTINCT sections that
    merely coincide in amount (a chance tie, two $0 lines) never merge, so no real figure is
    dropped. A section NetSuite names differently from its total (a P&L "Ordinary
    Income/Expense" / "Net Ordinary Income") is simply not folded; both coherent rows survive.

    Step 2 — everything that fits is shown as-is. ONLY when the section set overflows
    ``_STATEMENT_TABLE_MAX`` do we pick an altitude: the SHALLOWEST level reaching
    ``_STATEMENT_MIN_SECTIONS`` (a cash flow keeps its top-level flows), falling through to the
    next level when the shallow one is too thin.

    Step 3 — the TRUE closing conclusion (``picked[-1]``, last in statement order) ALWAYS
    survives, even if it sits deeper than the selected level (the T2-gate r5 invariant). If
    still over the cap, keep the shallowest lines + the conclusion (evict deepest-then-latest —
    never a shallow section, never the close), in statement order."""
    conclusion = picked[-1]  # true statement close (highest index) — never dropped
    fold: set = set()
    for i, p in enumerate(picked):
        num = _coerce_number(p[3])
        if p is conclusion or num is None or num == 0:
            continue
        j = next((k for k in range(i + 1, len(picked)) if picked[k][0] <= p[0]), None)
        if (
            j is not None
            and picked[j] is not conclusion
            and picked[j][0] == p[0]
            and _coerce_number(picked[j][3]) == num
            and _same_section(p[2], picked[j][2])
        ):
            fold.add(i)  # the header folds into its subtotal (the later of the pair is kept)
    kept = [p for i, p in enumerate(picked) if i not in fold]  # already in statement order
    if len(kept) <= _STATEMENT_TABLE_MAX:
        chosen = kept  # it all fits — show every section, no altitude trimming
    else:
        by_level: dict[int, list] = defaultdict(list)
        for p in kept:
            by_level[p[0]].append(p)
        levels = sorted(by_level)
        chosen = kept
        acc: list = []
        for lvl in levels:
            acc.extend(by_level[lvl])
            if len(acc) >= _STATEMENT_MIN_SECTIONS or lvl == levels[-1]:
                chosen = sorted(acc, key=lambda p: p[1])
                break
    if conclusion[1] not in {p[1] for p in chosen}:  # the close may sit deeper than the level
        chosen = sorted(chosen + [conclusion], key=lambda p: p[1])
    if len(chosen) > _STATEMENT_TABLE_MAX:
        body = [p for p in chosen if p[1] != conclusion[1]]
        body = sorted(body, key=lambda p: (p[0], p[1]))[: _STATEMENT_TABLE_MAX - 1]
        chosen = sorted(body + [conclusion], key=lambda p: p[1])
    return chosen


def _curate_statement(cols: list, rows: list, line_meta, currency_columns: list) -> tuple[list, list] | None:
    """Curate a statement-shaped table to its NAMED section-summary lines.

    Returns ``(statement_rows, callout_sections)`` or None when the payload is not
    statement-shaped (missing/misaligned ``line_meta``, or fewer than
    ``_MIN_STATEMENT_LINES`` qualifying lines) — the caller then falls back to the general
    top-K floor.

    REAL ns_runReport statements (CF/P&L/BS) tag section/total rows via ``line_meta``
    ``is_section`` (a non-null NetSuite ``label``) and mark the unnamed grand-total
    ``named=False``; the curated statement is then built from NAMED SECTION rows ONLY
    (``_select_statement_sections``) — detail accounts and the junk "Financial Row" grand
    total drop out STRUCTURALLY and the coherent section flows survive. The
    synthetic/indented path (only ``is_summary`` in line_meta) is unchanged
    (``_trim_statement_by_indent``). A line always needs a non-empty label (col 0) + a
    non-null amount (a "N/A" string still qualifies — a named value, rendered verbatim).
    Callouts are the LAST ``_STATEMENT_CALLOUT_MAX`` curated lines — a statement builds to
    its conclusions — accounting-formatted via the shared ``fmt_amount``.
    """
    if not isinstance(line_meta, list) or len(line_meta) != len(rows) or len(cols) < 2:
        return None
    amount_idx = _amount_index(cols, currency_columns)
    # Real statements carry section markers; the synthetic/indented path has only is_summary.
    # Detect which so the section-aware selection engages ONLY on real data.
    uses_sections = any(isinstance(m, dict) and "is_section" in m for m in line_meta)
    # Drop the unnamed grand-total (value=null "Financial Row") ONLY when the statement
    # otherwise names its sections via ``value`` (real NetSuite). A report that names rows via
    # ``label`` (value absent) has no such junk row, so keep every one of its sections.
    drop_unnamed = uses_sections and any(
        isinstance(m, dict) and m.get("is_section") and m.get("named") for m in line_meta
    )
    picked: list[tuple[int, int, list, object]] = []  # (level, index, row, qualified amount)
    for i, (row, meta) in enumerate(zip(rows, line_meta)):
        if not isinstance(meta, dict):
            continue
        if uses_sections:
            if not meta.get("is_section"):  # a section/subtotal/total row (not a detail account)
                continue
            if drop_unnamed and not meta.get("named", True):  # the junk unnamed grand-total
                continue
        elif not meta.get("is_summary"):
            continue
        label = str(row[0]).strip() if row and row[0] is not None else ""
        amount = row[amount_idx] if amount_idx < len(row) else None
        if not label or amount is None or (isinstance(amount, str) and not amount.strip()):
            continue
        try:
            # Parse like the producer (_line_hierarchy): int(float(...)) so a round-tripped
            # "1.0" string level trims consistently.
            level = int(float(meta.get("level", 0)))
        except (TypeError, ValueError):
            level = 0
        picked.append((level, i, row, amount))
    if len(picked) < _MIN_STATEMENT_LINES:
        return None
    picked = _select_statement_sections(picked) if uses_sections else _trim_statement_by_indent(picked)
    if len(picked) < _MIN_STATEMENT_LINES:
        # the header->subtotal fold can collapse a header+total-only statement to one row; fall
        # back to the general top-K floor rather than emit a single-line "curated statement".
        return None
    statement_rows = [row for _, _, row, _ in picked]
    callouts = [
        {
            "type": "metric_headline",
            "label": str(row[0]),
            "value": fmt_amount(amount),  # the amount the line QUALIFIED on — no re-derivation
            "unit": "",
            "period": "",
            "definition_version": None,
        }
        for _, _, row, amount in picked[-_STATEMENT_CALLOUT_MAX:]
    ]
    return statement_rows, callouts


def _driver_rows(rows: list, line_meta: list, cols: list, currency_columns: list) -> list:
    """The top-``_REPORT_TABLE_TOP_K`` DETAIL (leaf) rows by ``|amount|``, original
    statement order preserved — the comparable drivers for a statement chart.
    Summary/subtotal/grand-total lines are EXCLUDED: charting a subtotal next to its own
    detail lines double-counts, and a grand-total bar dwarfs every real driver (the live
    bar-soup symptom). Structural (line_meta + amounts), never by label."""
    amount_idx = _amount_index(cols, currency_columns)
    leaves: list[tuple[float, int, list]] = []
    for i, (row, meta) in enumerate(zip(rows, line_meta)):
        if not isinstance(meta, dict):
            continue
        # A real leaf account (``is_leaf``: named, immediately followed by its detail marker)
        # is the chartable driver — this excludes both section/subtotal rows AND account
        # GROUPS (whose sub-accounts would double-count). On the synthetic/indented path
        # (no ``is_leaf`` signal) fall back to "any non-summary named row" (pre-existing).
        if "is_leaf" in meta:
            if not meta.get("is_leaf"):
                continue
        elif meta.get("is_summary"):
            continue
        label = str(row[0]).strip() if row and row[0] is not None else ""
        amount = _coerce_number(row[amount_idx]) if amount_idx < len(row) else None
        if not label or amount is None:
            continue
        leaves.append((abs(amount), i, row))
    top = sorted(leaves, key=lambda t: t[0], reverse=True)[:_REPORT_TABLE_TOP_K]
    return [row for _, _, row in sorted(top, key=lambda t: t[1])]


def _build_tabular_chart(
    cols: list, rows: list, *, chart_type: str | None, title: str | None, value_columns: list | None = None
) -> "ChartData | None":
    """Build ChartData from a tabular payload: first column = x-axis, numeric columns =
    y-axis series. When ``value_columns`` is given the y-axes are restricted to those
    (intersected with the numeric columns) — used by the auto-chart to plot ONLY the
    money columns, never a numeric dimension (year/id/count). Coerces charted cells
    ($,%,commas → float; a non-parsing cell → 0.0). Returns None when nothing is
    chartable. Shared by the explicit `chart` section branch and the auto-chart injector."""
    if not cols or not rows:
        return None
    numeric_cols = _numeric_value_columns(cols, rows)
    if value_columns is not None:
        numeric_cols = [c for c in numeric_cols if c in value_columns]
    if not numeric_cols:
        return None
    numeric_set = set(numeric_cols)

    def _row_dict(r):
        d = dict(zip(cols, r))
        for c in numeric_set:
            d[c] = _coerce_number(d.get(c)) or 0.0
        return d

    # Shape-driven defaults (Phase 4): a period/date-shaped x column is a TREND — default
    # to a line and say "trend" in the derived title. An explicit chart_type always wins
    # the default; the same parse also orients the line chronologically below.
    direction = _period_direction([r[0] if r else None for r in rows])
    time_series = direction is not None
    if title is None:
        title = f"{numeric_cols[0]} trend by {cols[0]}" if time_series else f"{numeric_cols[0]} by {cols[0]}"
    final_type = chart_type or ("line" if time_series else "bar")
    # A LINE must flow oldest→newest. A DESC (newest-first, "last N months") period series
    # would otherwise draw a mirror-image slope — cash that ROSE renders as a downward line
    # (T2 gate: minor). Reorder the CHART data only; the table keeps its own order. Bars are
    # order-agnostic, so only lines need it.
    plot_rows = list(reversed(rows)) if (final_type == "line" and direction == "desc") else rows
    return ChartData(
        chart_type=final_type,
        title=title,
        x_axis={"label": cols[0], "key": cols[0]},
        y_axes=[{"label": c, "key": c} for c in numeric_cols],
        data=[_row_dict(r) for r in plot_rows],
    )


def _auto_chart_section(resolved: dict, *, drivers: list | None = None, label: str | None = None) -> dict | None:
    """A chart of a resolved table's rows, so every data-heavy report visualizes its
    drivers even when the model composed no chart.

    ``drivers`` (statement tables): chart the top LEAF detail rows instead of the
    curated summary lines — a bar of comparable drivers, never subtotal/grand-total
    soup. Otherwise chart the table's own (already curated) rows, with the chart TYPE
    picked by the x column's shape (period/date-like → line trend). ``label`` (the
    model-supplied section title) titles the chart; without it a descriptive title is
    derived deterministically.

    Charts ONLY the producer-tagged currency columns (the real measures). With no tag,
    charts a SINGLE numeric column (unambiguous), but skips a multi-numeric untagged
    table — guessing which numeric column is the measure risks plotting a dimension
    (year/id/count) as a misleading series. None when too small / nothing safe to plot."""
    if resolved.get("type") != "table":
        return None
    # A statement table ALWAYS arrives with a drivers list (possibly empty — a collapsed
    # all-summary statement has no leaves). It must chart drivers or NOTHING: falling
    # back to the summary rows would bar-chart Net Change beside the sections it sums
    # and an Ending-Cash balance beside flows — the exact soup this exists to kill.
    if drivers is not None:
        rows = drivers
    else:
        rows = resolved.get("rows", [])
    cols = resolved.get("columns", [])
    # Curation bounds this to K (<<100), but guard the DoS-shape independently so a future
    # higher _REPORT_TABLE_TOP_K can't bake a multi-MB SVG (same cap the chart branch uses).
    if not (_MIN_AUTO_CHART_ROWS <= len(rows) <= _MAX_CHART_POINTS):
        return None
    numeric_cols = _numeric_value_columns(cols, rows)
    currency = _currency_in(resolved, numeric_cols)
    if currency:
        value_columns = currency
    elif len(numeric_cols) == 1:
        value_columns = numeric_cols
    else:
        return None  # ambiguous (or no) numeric measure → don't auto-chart a wrong series
    if drivers:
        title = label or _driver_title(len(rows), cols, currency)
        chart_type = "bar"  # leaf drivers are categorical by construction
    else:
        title = label or None  # falsy label ("" too) → _build_tabular_chart derives
        chart_type = None  # let the x column's shape decide (time-series → line)
    chart = _build_tabular_chart(cols, rows, chart_type=chart_type, title=title, value_columns=value_columns)
    if chart is None:
        return None
    return {"type": "chart", "svg": render_chart_svg(chart), "chart_type": chart.chart_type}


def fill_placeholders(text: str, resolver: Resolver) -> str:
    def _sub(m: re.Match) -> str:
        kind, ref = m.group(1), m.group(2).strip()
        rid, _, field = ref.partition(".")
        try:
            payload = resolver(rid)
        except Exception:
            return f"[unresolved: {kind}:{ref}]"
        if kind == "metric":
            field = field or "value"
            metric = _metric_fields(payload)
            if metric is not None:
                val = metric.get(field) if field in metric else metric.get("value")
                return str(val) if val is not None else f"[unresolved: {kind}:{ref}]"
        val = payload.get(field) if field else payload.get("value")
        return str(val) if val is not None else f"[unresolved: {kind}:{ref}]"

    return _PLACEHOLDER.sub(_sub, text)


def _resolve_data_section(s: dict, resolver: Resolver) -> dict:
    try:
        payload = resolver(s["result_id"])
    except Exception as exc:
        return {"type": "error", "reason": f"{s.get('result_id')}: {exc}"}
    if s["type"] == "table":
        cols, rows = payload.get("columns", []), payload.get("rows", [])
        # The TRUE pre-curation count drives the "first K of N" note: prefer the upstream
        # tool's reported row_count, else the resolved row length.
        true_row_count = payload.get("row_count", len(rows))
        upstream_truncated = bool(payload.get("truncated", False))
        line_meta = payload.get("line_meta")
        currency_columns = _currency_in(payload, cols)
        has_summary_lines = _has_summary_lines(line_meta, rows, cols, currency_columns)
        # STATEMENT treatment (Phase 3): a statement-shaped payload (line_meta with
        # amount-bearing summary lines) curates to its named section-summary lines +
        # marquee callouts instead of the positional top-K — MECHANICALLY: a model
        # `select` is IGNORED here (deterministic-first; honoring a projection could
        # only lose the amount column or scramble the label, rendering mixed rows —
        # T2 gate r5). Gated on the payload NOT being upstream-truncated: a tail-cut
        # payload may be missing the statement's CONCLUDING lines, so claiming
        # "curated statement" over it — and promoting interior subtotals as marquee
        # "conclusions" — would be dishonest; the top-K floor's note discloses instead.
        if has_summary_lines and not upstream_truncated:
            curated_stmt = _curate_statement(cols, rows, line_meta, currency_columns)
            if curated_stmt is not None:
                statement_rows, callouts = curated_stmt
                return {
                    "type": "table",
                    "columns": cols,
                    "rows": statement_rows,
                    "row_count": true_row_count,
                    # Derived, never hardcoded: False when EVERY source row qualified
                    # (nothing was dropped by curation). Compared against the resolved
                    # rows, not row_count (which may be a numeric STRING in MCP shapes).
                    "truncated": len(statement_rows) < len(rows),
                    "curation": "statement",
                    "currency_columns": currency_columns,
                    # Internal hand-offs to assemble_spec (consumed there, then
                    # stripped) — never persisted into the frozen spec. Callouts render
                    # ABOVE the table; drivers feed the auto-chart with the top LEAF
                    # detail rows (the statement table shows the summaries, the chart
                    # shows the movers — never subtotal+detail double-count).
                    "statement_callouts": callouts,
                    "statement_drivers": _driver_rows(rows, line_meta, cols, currency_columns),
                }
        # The top-K floor. `select` is honored here (non-statement tables).
        if s.get("select"):
            idx = [cols.index(c) for c in s["select"] if c in cols]
            cols = [cols[i] for i in idx]
            rows = [[r[i] for i in idx] for r in rows]
            currency_columns = _currency_in(payload, cols)
        pre_curation_rows = rows
        # Curate to the first K rows — a report shows top numbers, not a dump. This also
        # bounds the rendered table (no separate anti-bloat cap needed: K << any payload).
        rows, curated = _curate_table_rows(rows, _REPORT_TABLE_TOP_K)
        out: dict = {
            "type": "table",
            "columns": cols,
            "rows": rows,
            "row_count": true_row_count,
            "truncated": upstream_truncated or curated,
            "currency_columns": currency_columns,
        }
        if has_summary_lines:
            # A statement-shaped result that fell to the positional floor must not
            # auto-chart its first-K rows (mixed subtotals + details — the bar-soup).
            # Chart honest LEAF drivers instead when they are computable; ONLY a
            # tail-cut payload gets an empty list (its true top movers may live in the
            # cut tail — T2 gate r5: a blanket [] killed the PR #151 guaranteed chart
            # on ordinary detail reports). `select` projects columns, never rows, so
            # line_meta stays row-aligned; a projection that lost the amount column
            # simply yields no qualifying leaves.
            out["statement_drivers"] = (
                [] if upstream_truncated else _driver_rows(pre_curation_rows, line_meta, cols, currency_columns)
            )
        return out
    if s["type"] == "metric_headline":
        # Prefer the real blessed-metric row shape; fall back to top-level reads so the
        # hand-rolled unit-test stub ({value,unit,period} keys) still resolves.
        metric = _metric_fields(payload)
        if metric is not None:
            return {
                "type": "metric_headline",
                "label": s.get("label") or metric.get("label") or "",
                "value": metric.get("value", ""),
                "unit": metric.get("unit", ""),
                "period": metric.get("period", ""),
                "definition_version": metric.get("definition_version"),
            }
        return {
            "type": "metric_headline",
            "label": s.get("label") or payload.get("display_name", ""),
            "value": payload.get("value", ""),
            "unit": payload.get("unit", ""),
            "period": payload.get("period", ""),
            "definition_version": payload.get("definition_version"),
        }
    if s["type"] == "chart":
        cd = payload.get("chart_data")
        if cd is None:  # build a minimal ChartData from a tabular payload
            # A single-number blessed-metric result is headline material, not a chart:
            # its only numeric column is 'Value' (Unit/Period would float()->0.0 nonsense
            # bars). Refuse deterministically rather than persist a misleading frozen chart.
            if _metric_fields(payload) is not None:
                return {"type": "error", "reason": "metric results are headline material, not charts"}
            cols = payload.get("columns", [])
            rows = payload.get("rows", [])
            # An explicit chart over a STATEMENT payload (line_meta aligned) charts the
            # top LEAF detail drivers — a subtotal/grand-total bar next to its own
            # details double-counts and dwarfs the real movers (the live bar-soup).
            default_title = None
            line_meta = payload.get("line_meta")
            stmt_currency = _currency_in(payload, cols)
            # Driver substitution applies ONLY where the hierarchy demands it: a
            # statement mixing amount-bearing summary AND detail lines (charting both
            # double-counts). An all-DETAIL listing — including one with a bare
            # amount-less header row — has no such hazard and keeps the pre-existing
            # chart-every-row behavior (the renderer's own cap + note handle
            # legibility); a silent magnitude top-12 substitution there would drop
            # rows the model explicitly asked to chart (T2 gate r3/r5: major).
            if _has_summary_lines(line_meta, rows, cols, stmt_currency):
                # Honesty gate (mirrors the table branch): drivers ranked over the
                # stored HEAD of a tail-cut statement are dishonest — the true top
                # movers may live in the cut tail. Refuse deterministically.
                if payload.get("truncated"):
                    return {
                        "type": "error",
                        "reason": (
                            "statement was truncated upstream — chart a smaller aggregated or per-period result instead"
                        ),
                    }
                drivers = _driver_rows(rows, line_meta, cols, stmt_currency)
                if len(drivers) < _MIN_AUTO_CHART_ROWS:
                    # A collapsed (all-summary) statement has no comparable leaves —
                    # charting its summary rows would double-count (Net Change beside
                    # the sections it sums) and dwarf (an Ending-Cash balance beside
                    # flows). Refuse deterministically rather than render soup.
                    return {
                        "type": "error",
                        "reason": (
                            "statement has no comparable detail lines to chart — "
                            "chart a per-period or aggregated result instead"
                        ),
                    }
                rows = drivers
                # Titled after the SAME column the drivers were ranked by — one shared
                # derivation (_driver_title) for every driver-chart site.
                default_title = _driver_title(len(rows), cols, stmt_currency)
            # Row cap: a chart over tens of thousands of rows bakes a multi-MB SVG into
            # the report (DoS-shape). Refuse deterministically before building anything.
            if len(rows) > _MAX_CHART_POINTS:
                return {
                    "type": "error",
                    "reason": f"too many rows to chart ({len(rows)} > {_MAX_CHART_POINTS}) — aggregate first",
                }
            # Title: the model's label wins; else the driver title; else _build_tabular_chart
            # derives a descriptive one from the data (never the junk "Chart" default).
            chart = _build_tabular_chart(
                cols, rows, chart_type=s.get("chart_type"), title=s.get("label") or default_title
            )
            if chart is None:
                return {"type": "error", "reason": "no numeric columns to chart"}
        else:
            chart = ChartData.model_validate(cd)
            if s.get("chart_type"):
                chart.chart_type = s["chart_type"]
        return {"type": "chart", "svg": render_chart_svg(chart), "chart_type": chart.chart_type}
    return s


def assemble_spec(title: str, sections: list[dict], resolver: Resolver) -> dict:
    # Canonicalize the LLM's section-type aliases (text->narrative, data->table) HERE,
    # before we read s["type"], and validate in the same pass. The chat tool path
    # (report_export.execute -> compose_report -> assemble_spec) passes the raw LLM
    # dicts and never constructs ComposeRequest, so this is the only boundary that
    # reliably runs on the real path. Without it, a `text` section would fall through
    # to the heading/divider else-branch below and be dropped SILENTLY by the renderer.
    sections = normalize_and_validate_sections(sections)  # raises loudly on a truly-unknown type
    # Pass 1: resolve every section up front so chart success is known before we decide
    # which tables need an auto-chart (ordering-independent; a chart may precede its table).
    resolved_pairs: list[tuple[dict, dict]] = []
    for s in sections:
        if s["type"] == "narrative":
            resolved_pairs.append((s, {"type": "narrative", "markdown": fill_placeholders(s["markdown"], resolver)}))
        elif s["type"] in ("table", "metric_headline", "chart"):
            resolved_pairs.append((s, _resolve_data_section(s, resolver)))
        elif s["type"] == "heading":
            # TRUST BOUNDARY: rebuild passthrough sections through their SCHEMA so only
            # declared fields (with schema defaults/clamps) survive. The validated dicts
            # still carry any extra keys the model authored (pydantic ignores extras),
            # and pass 2 reads internal hand-off keys off resolved sections — a raw
            # (s, s) passthrough would let a model-authored `statement_callouts` inject
            # forged numbers / unescaped svg / a crashing value into the frozen report
            # (T2 gate: blocker). model_validate cannot fail here: the sections already
            # passed normalize_and_validate_sections above.
            resolved_pairs.append((s, HeadingSection.model_validate(s).model_dump()))
        else:  # divider
            resolved_pairs.append((s, DividerSection.model_validate(s).model_dump()))
    # result_ids the model SUCCESSFULLY charted itself — an explicit chart that resolved to
    # an error must NOT suppress the table's auto-chart fallback.
    model_charted_ids = {
        s.get("result_id") for s, r in resolved_pairs if s["type"] == "chart" and r.get("type") == "chart"
    }
    # Auto-chart dedupe is keyed by (result_id, select) so two tables over the SAME result
    # with DIFFERENT projections each get their own chart, but an identical table doesn't
    # double-chart. Statement callouts dedupe by the same key.
    auto_charted_keys: set = set()
    emitted_callout_keys: set = set()

    def _table_key(sec: dict) -> tuple:
        return (sec.get("result_id"), tuple(sec.get("select") or []))

    # Pass 2: emit, auto-appending a chart after any chartable table the model did not
    # successfully chart — a report visualizes its drivers; prompt guidance alone does not.
    provenance_sources: list[str] = []
    out_sections: list[dict] = []
    for s, resolved in resolved_pairs:
        # Statement treatment: marquee callout cards render ABOVE their curated
        # statement table; leaf DRIVERS feed the auto-chart. Pop both internal hand-off
        # keys so they never persist into the frozen spec_json. Gated to TABLE sections:
        # only the resolver's own freshly-built table dicts can carry these keys
        # legitimately (defense in depth on top of the sanitized heading/divider
        # passthrough above — never honor them off any section shape the model's dict
        # could reach).
        callouts = drivers = None
        if s["type"] == "table" and isinstance(resolved, dict):
            callouts = resolved.pop("statement_callouts", None)
            drivers = resolved.pop("statement_drivers", None)
        # Dedupe callouts exactly like auto-charts: a composition repeating the same
        # statement table must not stack a second identical row of marquee cards.
        if callouts and _table_key(s) not in emitted_callout_keys:
            out_sections.extend(callouts)
            emitted_callout_keys.add(_table_key(s))
        out_sections.append(resolved)
        if resolved.get("type") == "metric_headline" and resolved.get("definition_version") is not None:
            provenance_sources.append(f"metric:{s['result_id']}@v{resolved['definition_version']}")
        if (
            s["type"] == "table"
            and s.get("result_id") not in model_charted_ids
            and _table_key(s) not in auto_charted_keys
        ):
            auto = _auto_chart_section(resolved, drivers=drivers, label=s.get("label"))
            if auto is not None:
                out_sections.append(auto)
                auto_charted_keys.add(_table_key(s))
    return {"title": title, "sections": out_sections, "provenance": {"sources": provenance_sources}}


async def compose_report(
    db: AsyncSession,
    *,
    tenant_id,
    title: str,
    sections: list[dict],
    resolver: Resolver,
    created_by=None,
    source_run_id=None,
    accent_hsl: str = "240 6% 10%",
) -> dict:
    from app.models.report import Report

    spec = assemble_spec(title, sections, resolver)
    html = render_report_html(spec, accent_hsl=accent_hsl)
    await set_tenant_context(db, str(tenant_id))  # TOOL path: RLS context not pre-set
    report = Report(
        tenant_id=tenant_id,
        title=title,
        spec_json=spec,
        rendered_html=html,
        created_by=created_by,
        source_run_id=source_run_id,
    )
    db.add(report)
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.compose",
        actor_id=created_by,
        resource_type="report",
        resource_id=str(report.id),
    )
    # TURN ATOMICITY (gate cluster A): do NOT commit here. compose_report runs as a
    # chat tool on the orchestrator's SHARED in-turn session; the orchestrator
    # commits exactly ONCE at end of turn, so the report row + its audit persist
    # atomically with the turn. A mid-turn commit would survive even if a later
    # step rolls back, orphaning a committed report under a failed turn. We flush
    # (above) so report.id is assigned for the audit row + the returned id.
    return {"report_id": str(report.id), "title": title, "section_count": len(spec["sections"])}
