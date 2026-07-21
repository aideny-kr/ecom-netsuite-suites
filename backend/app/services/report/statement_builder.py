"""Pure Decimal statement model builder.

``build_statement_model(section, payloads) -> dict`` turns resolved ``financial_statement``
report payloads (see ``playbooks.build_playbook_recipe`` for the section shape this consumes)
into the render-ready MODEL described in the Task 2 brief. Task 3's renderer consumes this
MODEL VERBATIM: every formatted-string field (money/pct/pp/narrative) is FINAL FORM — the
renderer only HTML-escapes and wraps, it never formats a number. The only non-string numeric
fields are ``kpis[].spark`` and ``trend.series[].values``, which stay raw ``Decimal`` because
they feed SVG geometry, not text.

No I/O anywhere in this module: no DB, no network, no LLM, no clock reads. All arithmetic is
``decimal.Decimal``; floats/strings are converted via ``Decimal(str(v))`` ONLY at the payload
parse boundary (``_to_decimal``) — nothing past that boundary touches a float. Money rounds to
whole dollars via ``ROUND_HALF_UP`` and percentages/pp round to 1 decimal, both ONLY at format
time (``fmt_money``/``fmt_pct``/etc.) — internal running totals keep full Decimal precision so
a threshold comparison (e.g. "GP margin moved >= 0.3pp") never suffers a display-rounding
false negative/positive.

Determinism: identical inputs always produce a byte-identical model. Dict/section iteration
order is either fixed (the statement taxonomy) or explicitly sorted (account rows by
acctnumber) — never dict-insertion-order-dependent in a way that could vary.

--------------------------------------------------------------------------------------------
Degradation contract (binding — this is what Task 4's resolver must honor when wiring
``assemble_spec`` to this builder):

``payloads`` maps rid -> a resolved payload in the ``extract_result_payload``-normalized shape
(``{"columns": [...], "rows": [[...], ...]}`` primary; a ``{"items": [{...}, ...]}``
list-of-dicts fallback is also accepted, defensively, for a payload that never passed through
``extract_result_payload``).

- ``r1`` (``section["result_id"]``) MUST resolve to a parseable payload with real accounts —
  a missing/absent/malformed r1 raises ``ValueError`` (report compose's fail-closed semantics
  depend on this: a partially-built statement must never render as if it succeeded).
- Every OTHER rid referenced by ``section["compare"]`` (prior/yoy/trend) may be:
    (a) entirely ABSENT from ``payloads`` (Task 4 never resolved it / it wasn't referenced), OR
    (b) present but ``payload.get("success") is False`` (the source tool call failed), OR
    (c) present but structurally unparseable (neither columns+rows nor items).
  Any of these degrades ONLY the fields that specific comparison feeds (MoM/YoY/trend/spark/
  watch/highlights/period labels) to ``None``/``[]`` — it NEVER raises. A compare failure must
  never take down the whole statement.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# Module constants — watch/highlight thresholds (named, per the brief)
# ---------------------------------------------------------------------------

#: Rule 1 — GP margin MoM |delta| >= this many percentage points fires a watch item.
GP_MARGIN_WATCH_THRESHOLD_PP = Decimal("0.3")
#: Rule 2 — an account MoM |delta| >= this % of current-period revenue is a "mover".
ACCOUNT_MOVER_THRESHOLD_PCT_OF_REVENUE = Decimal("1")
#: Rule 2 caps at this many movers (ranked by |delta| descending).
MAX_ACCOUNT_MOVERS = 2
#: Watch list never exceeds this many items (rules fire in priority order 1/2/3; with the
#: above caps 1 + 2 + 1 = 4 can never be exceeded, but callers should not rely on that).
MAX_WATCH_ITEMS = 4
#: A driver-attribution highlight only fires when the driving account's |delta| is at least
#: this % of current-period revenue.
HIGHLIGHT_THRESHOLD_PCT_OF_REVENUE = Decimal("0.5")
#: Highlights list never exceeds this many items (H1 NI, H2 GP margin, H3 OpEx).
MAX_HIGHLIGHTS = 3
#: Trailing-window width for the NI-margin best/worst watch rule (rule 3) and for sparklines.
TREND_WINDOW_MONTHS = 6
#: Mirrors `FETCH FIRST 5000 ROWS ONLY` in the three statement SQL templates
#: (netsuite_financial_report.py REPORT_TEMPLATES: income_statement, balance_sheet,
#: trial_balance). When a statement's OWN row count reaches this cap, the underlying SQL
#: may have silently truncated a larger tenant's real account list -- corrupting totals/NI/
#: the balance check under a UI that otherwise implies "nothing truncated". Keep these two
#: numbers in sync if either changes.
STATEMENT_ROW_CAP = 5000

_WHOLE_DOLLAR = Decimal("1")
_ONE_DP = Decimal("0.1")
_HUNDRED = Decimal("100")
MINUS = "−"  # typographic minus, per the brief's own "−0.4pp" example

_IS_SECTION_ORDER = ["1-Revenue", "2-Other Income", "3-COGS", "4-Operating Expense", "5-Other Expense"]
_IS_SECTION_LABELS = {
    "1-Revenue": "Revenue",
    "2-Other Income": "Other Income",
    "3-COGS": "Cost of Goods Sold",
    "4-Operating Expense": "Operating Expense",
    "5-Other Expense": "Other Expense",
}
_IS_ALWAYS_REDUCES = frozenset({"3-COGS", "4-Operating Expense", "5-Other Expense"})

_BS_SECTION_ORDER = ["1-Assets", "2-Liabilities", "3-Equity"]
_BS_SECTION_LABELS = {"1-Assets": "Assets", "2-Liabilities": "Liabilities", "3-Equity": "Equity"}

_STATEMENT_TITLES = {
    "income_statement": "Income Statement",
    "balance_sheet": "Balance Sheet",
    "trial_balance": "Trial Balance",
}


# ---------------------------------------------------------------------------
# Formatting helpers (pure) — renderer stays dumb, these produce FINAL-FORM strings.
# ---------------------------------------------------------------------------


def _normalize_zero(q: Decimal) -> Decimal:
    """Decimal preserves the sign bit through ``quantize`` (``str(Decimal('-0.0'))`` ==
    ``'-0.0'``) even though ``Decimal('-0.0') == 0`` numerically. Clear it so a value that
    rounds to zero never renders a leading minus."""
    return abs(q) if q == 0 else q


def fmt_money(value: Decimal, *, reduces_profit: bool = False) -> str:
    """Whole-dollar, thousands-separated. ``reduces_profit=True`` renders the ABSOLUTE value
    in parens (the GAAP convention for an expense/contra line, whether the underlying Decimal
    itself is a positive magnitude or an already-negative contra amount) — e.g. COGS
    ``1700000`` -> ``"($1,700,000)"``; a negative contra-revenue line ``-250000`` with
    ``reduces_profit=True`` -> ALSO ``"($250,000)"``, never a double-negative. Otherwise
    renders the value's own natural sign: positive -> ``"$X"``, negative -> ``"−$X"``."""
    q = _normalize_zero(value.quantize(_WHOLE_DOLLAR, rounding=ROUND_HALF_UP))
    if reduces_profit:
        return f"(${abs(q):,})"
    if q < 0:
        return f"{MINUS}${abs(q):,}"
    return f"${q:,}"


def fmt_money_delta(value: Decimal) -> str:
    """A CHANGE amount — always signed (never parens): ``+$317,000`` / ``−$52,000`` /
    ``$0``. Deltas are framed as "this moved," not "this reduces profit," so they never use
    the parens convention even when the underlying line does."""
    q = _normalize_zero(value.quantize(_WHOLE_DOLLAR, rounding=ROUND_HALF_UP))
    if q > 0:
        return f"+${q:,}"
    if q < 0:
        return f"{MINUS}${abs(q):,}"
    return "$0"


def fmt_pct(value: Decimal) -> str:
    """An absolute percentage (margin, % of revenue), 1 decimal, natural sign: ``"25.9%"`` /
    ``"−1.9%"``. Never signed with a leading ``+`` (that's ``fmt_pct_delta``)."""
    q = _normalize_zero(value.quantize(_ONE_DP, rounding=ROUND_HALF_UP))
    if q < 0:
        return f"{MINUS}{abs(q)}%"
    return f"{q}%"


def fmt_pct_delta(value: Decimal) -> str:
    """A percentage CHANGE, 1 decimal, always signed: ``"+2.4%"`` / ``"−2.8%"`` /
    ``"0.0%"``."""
    q = _normalize_zero(value.quantize(_ONE_DP, rounding=ROUND_HALF_UP))
    if q > 0:
        return f"+{q}%"
    if q < 0:
        return f"{MINUS}{abs(q)}%"
    return "0.0%"


def fmt_pp(value: Decimal) -> str:
    """A percentage-POINT change (margin delta), 1 decimal, always signed: ``"+0.7pp"`` /
    ``"−0.4pp"`` / ``"0.0pp"``."""
    q = _normalize_zero(value.quantize(_ONE_DP, rounding=ROUND_HALF_UP))
    if q > 0:
        return f"+{q}pp"
    if q < 0:
        return f"{MINUS}{abs(q)}pp"
    return "0.0pp"


def _row_cap_watch_item(row_count: int) -> dict | None:
    """A ``warn`` watch chip when a statement's OWN row count lands on ``STATEMENT_ROW_CAP``
    -- see that constant's comment. ``None`` (no chip) below the cap."""
    if row_count < STATEMENT_ROW_CAP:
        return None
    return {"tone": "warn", "text": f"row cap reached — totals may be incomplete ({STATEMENT_ROW_CAP} accounts)"}


#: Ordered (dict-insertion order = display order) so the missing-compare watch items
#: below always list prior/yoy/trend in that priority, never dict-iteration-order luck.
_COMPARE_LABELS = {"prior": "Prior-period", "yoy": "Year-over-year", "trend": "Trend"}


def _missing_compare_watch_items(compare: dict, resolved: dict[str, list | None]) -> list[dict]:
    """Explicit in-statement signal (T2 gate M1) per expected-but-unresolved comparison:
    the builder already knows ``section["compare"]``'s INTENT (which comparisons this
    statement is supposed to carry) vs which of those rids actually resolved
    (``resolved``, keyed the same as ``compare``) — surface a ``warn`` chip for each gap
    rather than leaving the reader to infer "no prior column" means "prior unavailable
    this run" vs "not applicable to this statement type" (BS/TB never have a yoy/trend
    key in ``compare`` at all, so those never fire for them). A comparison the recipe
    never asked for (absent from ``compare``) never fires, regardless of ``resolved``."""
    items = []
    for key, label in _COMPARE_LABELS.items():
        if compare.get(key) and resolved.get(key) is None:
            items.append({"tone": "warn", "text": f"{label} comparison unavailable this run"})
    return items


# ---------------------------------------------------------------------------
# Parse boundary — the ONLY place a float/str amount becomes a Decimal.
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    """Parse boundary. Raises ``ValueError`` (never ``decimal.InvalidOperation``, which is
    NOT a ``ValueError`` subclass and would otherwise escape the ``except ValueError``
    fail-closed seam every caller up the stack relies on) for anything that doesn't parse
    to a FINITE Decimal -- a non-numeric string, or a numeric-looking one like "nan"/"inf"
    that constructs fine but would explode later at ``.quantize()`` instead."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"amount is not finite: {value!r}")
        return value
    if isinstance(value, bool):
        raise ValueError(f"cannot treat a bool as a monetary amount: {value!r}")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse amount as Decimal: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"amount is not finite: {value!r}")
    return d


def _rows_from_payload(payload: dict) -> list[dict]:
    """Normalize a resolved payload into a list of dict rows keyed by column name. Primary:
    ``columns`` + ``rows`` (list-of-lists, the ``extract_result_payload`` shape). Defensive
    fallback: ``items`` (list-of-dicts). Raises ``ValueError`` when the payload has neither —
    the caller decides whether that failure is fatal (r1) or a silent degrade (compares)."""
    columns = payload.get("columns")
    rows = payload.get("rows")
    if isinstance(columns, list) and isinstance(rows, list):
        return [dict(zip(columns, row)) for row in rows]
    items = payload.get("items")
    if isinstance(items, list) and all(isinstance(item, dict) for item in items):
        return items
    raise ValueError("payload has neither columns+rows nor an items list")


def _is_truncated(payload: dict, rows: list[dict]) -> bool:
    """T2 gate B1: true when ``payload`` is a WELL-SHAPED but PARTIAL account list -- either
    the source declares ``truncated`` itself (an under-paginated OAuth1 SuiteQL call, an
    already-capped upstream payload), or its ``row_count`` (the TRUE pre-cap total a layer
    like ``tool_call_results._cap_stored_rows`` preserves) exceeds the rows actually
    present. Distinct from ``_row_cap_watch_item``, which fires when a statement's row
    count lands exactly on ``STATEMENT_ROW_CAP`` (the SQL's OWN cap, never flagged
    ``truncated`` by the source) -- this catches the extraction layer cutting rows BELOW
    what the source actually returned, the round-2 B1 bug (a 2000-row extraction cap
    silently corrupting a >2000-account statement)."""
    if payload.get("truncated"):
        return True
    row_count = payload.get("row_count")
    return isinstance(row_count, int) and row_count > len(rows)


def _resolve_rows(
    payloads: dict[str, dict], rid: str | None, *, amount_cols: tuple[str, ...] = (), cap_degrades: bool = True
) -> list[dict] | None:
    """Resolve a compare rid to its parsed rows, or ``None`` on ANY failure — absent, marked
    failed, structurally unparseable, a well-shaped but EMPTY row set (T2 gate M-B — a
    derived compare period can legitimately return zero rows; rendering deltas/margins
    against a phantom $0 prior is misleading, so it degrades exactly like an absent
    source), a TRUNCATED payload (T2 gate B1 — see ``_is_truncated``; a PARTIAL prior/yoy/
    trend account list would compute silently wrong deltas/margins/sparklines, worse than
    no comparison at all), a row count landing AT/ABOVE ``STATEMENT_ROW_CAP`` when
    ``cap_degrades`` is true (T2 gate F-2 — the SQL cap is baked INSIDE the query text, so
    a capped source's own ``totalResults`` reflects the CAPPED count, not the true total —
    ``_is_truncated`` can never see this; a compare source AT the cap is unreliable, not
    honest-at-the-boundary data the way r1 is, since we can't tell whether a real account
    list got cut short), OR (when ``amount_cols`` is given) a row whose value in one of
    those columns doesn't parse to a finite Decimal. This is the degrade-never-raise
    boundary for compare sources: without the ``amount_cols`` pre-validation, a malformed
    amount in a PRIOR/YOY/TREND source would only be discovered much later, deep inside a
    totals/section builder that doesn't wrap its calls in try/except — crashing the WHOLE
    statement instead of degrading just this one comparison. r1 uses ``_require_rows``
    instead (raises — the primary source must fail closed, never silently degrade).

    ``cap_degrades=False`` opts a caller OUT of the generic at-cap degrade so it can apply
    its OWN cap handling instead — the IS builder passes this for the trend rid, which
    needs a trend-specific chip ("trend source at row cap — trend omitted") rather than
    the generic missing-compare wording this function's default triggers."""
    if not rid:
        return None
    payload = payloads.get(rid)
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return None
    try:
        rows = _rows_from_payload(payload)
        if not rows:
            return None
        if _is_truncated(payload, rows):
            return None
        if cap_degrades and len(rows) >= STATEMENT_ROW_CAP:
            return None
        for col in amount_cols:
            for row in rows:
                _to_decimal(row.get(col))
        return rows
    except (ValueError, TypeError, AttributeError):
        return None


def _require_rows(payloads: dict[str, dict], rid: str) -> list[dict]:
    """r1's resolution boundary — raises on anything short of a real, non-empty,
    COMPLETE account list (never silently degrades; the caller must fail closed). A
    well-shaped but EMPTY row set (T2 gate M2) is rejected too: a statement with zero GL
    accounts at all is a data/connection problem (a genuinely quiet account still posts a
    $0 line — zero accounts means nothing posted to ANY tracked account type, which a real
    tenant never produces), not a legitimate render. A well-shaped but TRUNCATED row set
    (T2 gate B1 round 2 — see ``_is_truncated``) is rejected too: an extraction-layer cap
    (below the SQL's own ``STATEMENT_ROW_CAP``) that cut a real tenant's account list
    short would otherwise silently corrupt every total/NI/balance check downstream while
    rendering as if nothing were wrong. This is what lets compose/refresh fail closed on
    an empty OR partial statement instead of publishing a contentless or corrupted one."""
    payload = payloads.get(rid)
    if not isinstance(payload, dict):
        raise ValueError(f"required source {rid!r} is missing from payloads")
    if payload.get("success") is False:
        raise ValueError(f"required source {rid!r} failed: {payload.get('error')}")
    rows = _rows_from_payload(payload)  # raises ValueError on malformed shape
    if not rows:
        raise ValueError(f"required source {rid!r} has no accounts — statement would be empty")
    if _is_truncated(payload, rows):
        row_count = payload.get("row_count")
        total = row_count if isinstance(row_count, int) else len(rows)
        raise ValueError(
            f"required source {rid!r} account list truncated at {len(rows)} of {total} — "
            "statement cannot be computed completely"
        )
    return rows


def _account_sort_key(number: str):
    try:
        return (0, int(number))
    except (TypeError, ValueError):
        return (1, str(number))


def _account_index(rows: list[dict] | None, amount_col: str) -> dict[str, Decimal] | None:
    if rows is None:
        return None
    index: dict[str, Decimal] = {}
    for row in rows:
        number = row.get("acctnumber")
        if number is None:
            continue
        index[str(number)] = _to_decimal(row.get(amount_col))
    return index


def _section_sum(rows: list[dict] | None, amount_col: str, section_key: str) -> Decimal:
    if not rows:
        return Decimal("0")
    return sum(
        (_to_decimal(r.get(amount_col)) for r in rows if r.get("section") == section_key),
        Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Generic row primitives shared across statement types
# ---------------------------------------------------------------------------


def _pct_change(current: Decimal, prior: Decimal | None) -> str | None:
    """Percent change of ``current`` vs ``prior``, denominator ``abs(prior)`` (so a base that
    flips sign, e.g. NI swinging from a loss to a profit, reports a correctly SIGNED percent
    change instead of an artifact from dividing by a negative base). ``None`` when prior is
    unavailable OR exactly zero (an undefined percent change, even though the dollar delta is
    still well-defined and shown separately)."""
    if prior is None or prior == 0:
        return None
    return fmt_pct_delta((current - prior) / abs(prior) * _HUNDRED)


def _quad_row(
    label: str,
    current: Decimal,
    prior: Decimal | None,
    *,
    reduces_profit: bool,
    emph: str,
    pct_rev: str | None = None,
) -> dict:
    """``pct_rev`` (T2 gate M3, design rule #8): the common-size figure for THIS row, when
    the caller has a revenue base to compute it against — None for BS/TB (no revenue
    concept) and for any IS quad row the caller doesn't pass one for (defaults None,
    backward compatible with every pre-existing call site)."""
    return {
        "label": label,
        "current": fmt_money(current, reduces_profit=reduces_profit),
        "prior": fmt_money(prior, reduces_profit=reduces_profit) if prior is not None else None,
        "delta": fmt_money_delta(current - prior) if prior is not None else None,
        "delta_pct": _pct_change(current, prior),
        "reduces_profit": reduces_profit,
        "emph": emph,
        "pct_rev": pct_rev,
    }


def _pct_of(value: Decimal, base: Decimal | None) -> str | None:
    """``value`` as a % of ``base`` (1dp, via ``fmt_pct``), or ``None`` when there's no
    base to divide by (BS/TB have no revenue concept) or the base is exactly zero
    (undefined). Shared by KPI margin_pct and every common-size ``pct_rev`` figure — same
    formula everywhere, so a KPI card's margin and its statement-row twin never drift."""
    if base is None or base == 0:
        return None
    return fmt_pct(value / base * _HUNDRED)


def _kpi_row(
    key: str,
    label: str,
    current: Decimal,
    prior: Decimal | None,
    yoy: Decimal | None,
    *,
    margin_base: Decimal | None,
    spark: list[Decimal] | None,
    neutral: bool = False,
) -> dict:
    """``neutral`` (T2 gate minor[9], design rule #10): an IS KPI (revenue/profit) moving
    up is inherently favorable, but a BS/TB KPI (assets/liabilities/equity, debits/
    credits) moving up has no such inherent favorability — color is reserved EXCLUSIVELY
    for favorable/unfavorable, never decoration, so a BS/TB KPI's delta must render with
    an arrow but no color. Defaults False (every pre-existing IS call site unaffected)."""
    margin_pct = _pct_of(current, margin_base)
    return {
        "key": key,
        "label": label,
        "value": fmt_money(current),
        "neutral": neutral,
        "margin_pct": margin_pct,
        "mom_delta": fmt_money_delta(current - prior) if prior is not None else None,
        "mom_pct": _pct_change(current, prior),
        "yoy_pct": _pct_change(current, yoy),
        "spark": spark,
    }


def _build_sections(
    current_rows: list[dict],
    prior_rows: list[dict] | None,
    *,
    amount_col: str,
    section_order: list[str],
    section_labels: dict[str, str],
    reduces_profit_fn,
    revenue_total: Decimal | None,
) -> list[dict]:
    """Shared section/account-row builder for IS and BS (both grouped by a ``section``
    column). Union of current+prior accounts per section (an account present only on one
    side is treated as 0 on the other and STILL LISTED, per the alignment contract); rows
    sorted by acctnumber for deterministic order."""
    prior_index = _account_index(prior_rows, amount_col)

    current_by_section: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in current_rows:
        section = row.get("section")
        number = row.get("acctnumber")
        if section is None or number is None:
            continue
        current_by_section[section][str(number)] = row

    prior_only_by_section: dict[str, dict[str, dict]] = defaultdict(dict)
    if prior_rows is not None:
        for row in prior_rows:
            section = row.get("section")
            number = row.get("acctnumber")
            if section is None or number is None:
                continue
            number = str(number)
            if number in current_by_section.get(section, {}):
                continue
            prior_only_by_section[section][number] = row

    present_keys = [k for k in section_order if k in current_by_section or k in prior_only_by_section]
    sections_out = []
    for section_key in present_keys:
        accounts = []
        for number, row in current_by_section.get(section_key, {}).items():
            current_amt = _to_decimal(row.get(amount_col))
            prior_amt = None if prior_index is None else prior_index.get(number, Decimal("0"))
            reduces_profit = reduces_profit_fn(section_key, current_amt)
            pct_rev = None
            if revenue_total is not None and revenue_total != 0:
                pct_rev = fmt_pct(current_amt / revenue_total * _HUNDRED)
            accounts.append(
                {
                    "number": number,
                    "name": row.get("acctname", ""),
                    "current": fmt_money(current_amt, reduces_profit=reduces_profit),
                    # T2 gate F-3: the prior CELL's parens convention keys off the PRIOR
                    # amount's OWN sign (a sign-flipping contra-revenue account can be
                    # positive now but negative last period, or vice versa) -- reusing
                    # `reduces_profit` (computed from current_amt above) would render a
                    # negative prior with the plain natural-sign minus instead of parens.
                    "prior": (
                        fmt_money(prior_amt, reduces_profit=reduces_profit_fn(section_key, prior_amt))
                        if prior_amt is not None
                        else None
                    ),
                    "delta": fmt_money_delta(current_amt - prior_amt) if prior_amt is not None else None,
                    "pct_rev": pct_rev,
                    "reduces_profit": reduces_profit,
                }
            )
        for number, row in prior_only_by_section.get(section_key, {}).items():
            prior_amt = _to_decimal(row.get(amount_col))
            current_amt = Decimal("0")
            reduces_profit = reduces_profit_fn(section_key, current_amt)
            pct_rev = fmt_pct(Decimal("0")) if revenue_total not in (None, Decimal("0")) else None
            accounts.append(
                {
                    "number": number,
                    "name": row.get("acctname", ""),
                    "current": fmt_money(current_amt, reduces_profit=reduces_profit),
                    "prior": fmt_money(prior_amt, reduces_profit=reduces_profit_fn(section_key, prior_amt)),
                    "delta": fmt_money_delta(current_amt - prior_amt),
                    "pct_rev": pct_rev,
                    "reduces_profit": reduces_profit,
                }
            )
        accounts.sort(key=lambda a: _account_sort_key(a["number"]))

        current_subtotal = _section_sum(current_rows, amount_col, section_key)
        prior_subtotal = None if prior_rows is None else _section_sum(prior_rows, amount_col, section_key)
        subtotal = _quad_row(
            f"Total {section_labels.get(section_key, section_key)}",
            current_subtotal,
            prior_subtotal,
            reduces_profit=reduces_profit_fn(section_key, current_subtotal),
            emph="sub",
            pct_rev=_pct_of(current_subtotal, revenue_total),
        )
        sections_out.append(
            {
                "key": section_key,
                "label": section_labels.get(section_key, section_key),
                "accounts": accounts,
                "subtotal": subtotal,
            }
        )
    return sections_out


# ---------------------------------------------------------------------------
# Income statement
# ---------------------------------------------------------------------------


def _is_reduces_profit(section_key: str, amount: Decimal) -> bool:
    if section_key in _IS_ALWAYS_REDUCES:
        return True
    if section_key == "1-Revenue":
        return amount < 0
    return False  # 2-Other Income


def _is_totals(rows: list[dict] | None, amount_col: str = "amount") -> dict[str, Decimal] | None:
    """Section totals + IS derivations for one source's full row set. ``None`` propagates
    (source unavailable)."""
    if rows is None:
        return None
    revenue = _section_sum(rows, amount_col, "1-Revenue")
    other_income = _section_sum(rows, amount_col, "2-Other Income")
    cogs = _section_sum(rows, amount_col, "3-COGS")
    opex = _section_sum(rows, amount_col, "4-Operating Expense")
    other_expense = _section_sum(rows, amount_col, "5-Other Expense")
    gross_profit = revenue - cogs
    operating_income = gross_profit - opex
    net_income = operating_income + other_income - other_expense
    return {
        "revenue": revenue,
        "other_income": other_income,
        "cogs": cogs,
        "opex": opex,
        "other_expense": other_expense,
        "gross_profit": gross_profit,
        "operating_income": operating_income,
        "net_income": net_income,
    }


def _trend_periods(trend_rows: list[dict] | None) -> list[tuple[str, list[dict]]] | None:
    """Group r4 trend rows into ``(periodname, rows)`` buckets, sorted CHRONOLOGICALLY via
    ``_period_sort_key`` — NEVER a raw ``startdate`` string sort (see that function's
    docstring: live SuiteQL's "M/D/YYYY" format sorts wrong as a plain string, e.g.
    "10/1/2026" < "8/1/2026"). ``None`` when there is no trend source."""
    if trend_rows is None:
        return None
    buckets: dict[str, list[dict]] = defaultdict(list)
    first_date: dict[str, Any] = {}
    order: list[str] = []
    for row in trend_rows:
        pname = row.get("periodname")
        if pname is None:
            continue
        if pname not in buckets:
            order.append(pname)
            first_date[pname] = row.get("startdate")
        buckets[pname].append(row)
    order.sort(key=lambda p: _period_sort_key(p, first_date.get(p)))
    return [(p, buckets[p]) for p in order]


def _build_is_kpis(current: dict, prior: dict | None, yoy: dict | None, trend_buckets) -> list[dict]:
    spark: dict[str, list[Decimal] | None] = {
        "revenue": None,
        "gross_profit": None,
        "operating_income": None,
        "net_income": None,
    }
    if trend_buckets is not None:
        per_period = [_is_totals(rows) for _pname, rows in trend_buckets]
        spark["revenue"] = [p["revenue"] for p in per_period]
        spark["gross_profit"] = [p["gross_profit"] for p in per_period]
        spark["operating_income"] = [p["operating_income"] for p in per_period]
        spark["net_income"] = [p["net_income"] for p in per_period]

    def get(totals, key):
        return None if totals is None else totals[key]

    return [
        _kpi_row(
            "revenue", "Revenue", current["revenue"], get(prior, "revenue"), get(yoy, "revenue"),
            margin_base=None, spark=spark["revenue"],
        ),
        _kpi_row(
            "gross_profit",
            "Gross profit",
            current["gross_profit"],
            get(prior, "gross_profit"),
            get(yoy, "gross_profit"),
            margin_base=current["revenue"],
            spark=spark["gross_profit"],
        ),
        _kpi_row(
            "operating_income", "Operating income", current["operating_income"], get(prior, "operating_income"),
            get(yoy, "operating_income"), margin_base=current["revenue"], spark=spark["operating_income"],
        ),
        _kpi_row(
            "net_income", "Net income", current["net_income"], get(prior, "net_income"), get(yoy, "net_income"),
            margin_base=current["revenue"], spark=spark["net_income"],
        ),
    ]  # fmt: skip


def _build_is_watch(
    current: dict, prior: dict | None, current_rows: list[dict], prior_rows: list[dict] | None, trend_buckets
) -> list[dict]:
    """Rules 1/2 need ``prior`` (r2); rule 3 needs only ``trend_buckets`` (r4) -- each rule
    gates on its OWN required inputs independently, so a fixture with trend but no prior
    (or vice versa) still gets whichever rules it has the data for."""
    watch: list[dict] = []
    revenue = current["revenue"]

    # Rule 1: GP margin MoM |delta| >= threshold (raw precision, not display-rounded).
    if prior is not None and revenue != 0 and prior["revenue"] != 0:
        current_margin_raw = current["gross_profit"] / revenue * _HUNDRED
        prior_margin_raw = prior["gross_profit"] / prior["revenue"] * _HUNDRED
        delta_pp_raw = current_margin_raw - prior_margin_raw
        if abs(delta_pp_raw) >= GP_MARGIN_WATCH_THRESHOLD_PP:
            tone = "good" if delta_pp_raw > 0 else "warn"
            text = (
                f"GP margin {fmt_pp(delta_pp_raw)} MoM ({fmt_pct(current_margin_raw)} vs {fmt_pct(prior_margin_raw)})"
            )
            watch.append({"tone": tone, "text": text})

    # Rule 2: up to MAX_ACCOUNT_MOVERS accounts with |delta| >= threshold % of revenue.
    if prior is not None and revenue != 0:
        # T2 gate F-1: abs(revenue) -- a net-negative-revenue period (refund-heavy month)
        # must not flip the threshold negative, which would make `abs(delta) >=
        # threshold_dollars` vacuously true for every account regardless of magnitude.
        threshold_dollars = abs(revenue) * ACCOUNT_MOVER_THRESHOLD_PCT_OF_REVENUE / _HUNDRED
        prior_index = _account_index(prior_rows, "amount") or {}
        movers = []
        for row in current_rows:
            number = row.get("acctnumber")
            section = row.get("section")
            if number is None or section is None:
                continue
            number = str(number)
            current_amt = _to_decimal(row.get("amount"))
            prior_amt = prior_index.get(number, Decimal("0"))
            delta = current_amt - prior_amt
            if abs(delta) >= threshold_dollars:
                # Carry THIS row's own section through the tuple -- NetSuite does not
                # enforce acctname uniqueness, so re-deriving section from name later
                # (a lookup keyed only on the string) would silently mis-tone whichever
                # duplicate-named account it happened to match first.
                movers.append((abs(delta), row.get("acctname", ""), delta, prior_amt, section))
        movers.sort(key=lambda m: m[0], reverse=True)
        for _magnitude, name, delta, prior_amt, mover_section in movers[:MAX_ACCOUNT_MOVERS]:
            account_pct = _pct_change(prior_amt + delta, prior_amt)
            pct_text = account_pct if account_pct is not None else "n/a"
            # tone: an increase is "good" for revenue/other-income, "warn" for an
            # expense-shaped section (COGS/OpEx/OtherExpense); a decrease flips it.
            favorable_on_increase = mover_section in ("1-Revenue", "2-Other Income")
            improved = (delta > 0) == favorable_on_increase
            tone = "good" if improved else "warn"
            sign = "+" if delta > 0 else MINUS
            text = f"{name} {sign}${abs(delta.quantize(_WHOLE_DOLLAR, rounding=ROUND_HALF_UP)):,} MoM ({pct_text})"
            watch.append({"tone": tone, "text": text})

    # Rule 3: current NI margin is the trailing-window max/min.
    if trend_buckets is not None and len(trend_buckets) >= 2:
        per_period = [_is_totals(rows) for _pname, rows in trend_buckets]
        margins = [(p["net_income"] / p["revenue"] * _HUNDRED) if p["revenue"] != 0 else None for p in per_period]
        defined = [m for m in margins if m is not None]
        current_margin = margins[-1]
        if current_margin is not None and defined:
            if current_margin == max(defined):
                watch.append(
                    {"tone": "good", "text": f"NI margin best month in trailing 6 ({fmt_pct(current_margin)})"}
                )
            elif current_margin == min(defined):
                watch.append(
                    {"tone": "bad", "text": f"NI margin worst month in trailing 6 ({fmt_pct(current_margin)})"}
                )

    return watch[:MAX_WATCH_ITEMS]


def _build_is_highlights(
    current: dict, prior: dict | None, current_rows: list[dict], prior_rows: list[dict] | None
) -> list[str]:
    if prior is None:
        return []
    revenue = current["revenue"]
    if revenue == 0:
        return []
    # T2 gate F-1: abs(revenue) -- same reasoning as the account-mover gate above.
    threshold_dollars = abs(revenue) * HIGHLIGHT_THRESHOLD_PCT_OF_REVENUE / _HUNDRED
    prior_index = _account_index(prior_rows, "amount") or {}

    def largest_mover(section_filter=None):
        best = None  # (abs(delta), name, delta)
        for row in current_rows:
            number = row.get("acctnumber")
            section = row.get("section")
            if number is None or section is None:
                continue
            if section_filter is not None and section != section_filter:
                continue
            number = str(number)
            current_amt = _to_decimal(row.get("amount"))
            prior_amt = prior_index.get(number, Decimal("0"))
            delta = current_amt - prior_amt
            magnitude = abs(delta)
            if best is None or magnitude > best[0]:
                best = (magnitude, row.get("acctname", ""), delta)
        return best

    highlights: list[str] = []

    # H1: NI's largest single driver across ALL sections.
    ni_delta = current["net_income"] - prior["net_income"]
    driver = largest_mover()
    if driver is not None and driver[0] >= threshold_dollars:
        highlights.append(
            f"Net income {fmt_money_delta(ni_delta)} MoM, driven by {driver[1]} {fmt_money_delta(driver[2])}"
        )

    # H2: GP margin change with the largest COGS mover.
    if prior["revenue"] != 0:
        current_margin = current["gross_profit"] / revenue * _HUNDRED
        prior_margin = prior["gross_profit"] / prior["revenue"] * _HUNDRED
        gp_delta_pp = current_margin - prior_margin
        cogs_driver = largest_mover("3-COGS")
        if cogs_driver is not None and cogs_driver[0] >= threshold_dollars:
            highlights.append(
                f"Gross margin {fmt_pp(gp_delta_pp)} MoM, driven by {cogs_driver[1]} {fmt_money_delta(cogs_driver[2])}"
            )

    # H3: OpEx change with the largest OpEx mover.
    opex_delta = current["opex"] - prior["opex"]
    opex_driver = largest_mover("4-Operating Expense")
    if opex_driver is not None and opex_driver[0] >= threshold_dollars:
        opex_driver_delta_str = fmt_money_delta(opex_driver[2])
        highlights.append(
            f"Operating expense {fmt_money_delta(opex_delta)} MoM, driven by {opex_driver[1]} {opex_driver_delta_str}"
        )

    return highlights[:MAX_HIGHLIGHTS]


def _build_is_narrative(current: dict, prior: dict | None, period: str) -> list[str]:
    revenue = current["revenue"]
    revenue_str = fmt_money(revenue)
    ni_str = fmt_money(current["net_income"])
    margin_clause = ""
    if revenue != 0:
        margin_clause = f" ({fmt_pct(current['net_income'] / revenue * _HUNDRED)} margin)"
    mom_clause = ""
    gp_margin_str = fmt_pct(current["gross_profit"] / revenue * _HUNDRED) if revenue != 0 else "n/a"
    gp_delta_clause = ""
    opex_str = fmt_money(current["opex"])
    opex_delta_clause = ""
    if prior is not None:
        mom_pct = _pct_change(revenue, prior["revenue"])
        if mom_pct is not None:
            mom_clause = f", {mom_pct} month-over-month"
        if revenue != 0 and prior["revenue"] != 0:
            gp_delta_pp = (
                current["gross_profit"] / revenue * _HUNDRED - prior["gross_profit"] / prior["revenue"] * _HUNDRED
            )
            gp_delta_clause = f", {fmt_pp(gp_delta_pp)} MoM"
        opex_delta_str = fmt_money_delta(current["opex"] - prior["opex"])
        opex_delta_clause = f", {opex_delta_str} MoM"

    title = _STATEMENT_TITLES["income_statement"]
    sentence1 = (
        f"{title} for {period}: revenue was {revenue_str}{mom_clause}, "
        f"delivering net income of {ni_str}{margin_clause}."
    )
    sentence2 = (
        f"Gross margin was {gp_margin_str}{gp_delta_clause}, with operating expenses of {opex_str}{opex_delta_clause}."
    )
    return [sentence1, sentence2]


def _build_income_statement_model(section: dict, payloads: dict[str, dict]) -> dict:
    compare = section.get("compare") or {}
    period = section["period"]

    current_rows = _require_rows(payloads, section["result_id"])
    prior_rows = _resolve_rows(payloads, compare.get("prior"), amount_cols=("amount",))
    yoy_rows = _resolve_rows(payloads, compare.get("yoy"), amount_cols=("amount",))
    # T2 gate F-2: cap_degrades=False -- the generic at-cap degrade (_resolve_rows) would
    # fire the generic "Trend comparison unavailable this run" chip; the trend-at-cap
    # check just below needs to see the raw (still-at-cap) rows so it can degrade with
    # its OWN, more specific chip instead.
    trend_rows = _resolve_rows(payloads, compare.get("trend"), amount_cols=("amount",), cap_degrades=False)
    trend_at_cap = trend_rows is not None and len(trend_rows) >= STATEMENT_ROW_CAP
    if trend_at_cap:
        # a 6-month trend is account x period, so STATEMENT_ROW_CAP rows can represent
        # FAR fewer real accounts than a single-period source at the same cap -- treat the
        # whole trend as unreliable rather than risk a wrong chart/sparklines/rule-3 claim.
        trend_rows = None
    trend_buckets = _trend_periods(trend_rows)

    current = _is_totals(current_rows)
    prior = _is_totals(prior_rows)
    yoy = _is_totals(yoy_rows)

    kpis = _build_is_kpis(current, prior, yoy, trend_buckets)

    sections = _build_sections(
        current_rows,
        prior_rows,
        amount_col="amount",
        section_order=_IS_SECTION_ORDER,
        section_labels=_IS_SECTION_LABELS,
        reduces_profit_fn=_is_reduces_profit,
        revenue_total=current["revenue"],
    )

    revenue = current["revenue"]
    quad = [
        _quad_row("Revenue", revenue, None if prior is None else prior["revenue"], reduces_profit=False, emph="sub"),
        _quad_row(
            "Gross Profit",
            current["gross_profit"],
            None if prior is None else prior["gross_profit"],
            reduces_profit=False,
            emph="formula",
            pct_rev=_pct_of(current["gross_profit"], revenue),
        ),
        _quad_row(
            "Operating Income",
            current["operating_income"],
            None if prior is None else prior["operating_income"],
            reduces_profit=False,
            emph="formula",
            pct_rev=_pct_of(current["operating_income"], revenue),
        ),
        _quad_row(
            "Net Income",
            current["net_income"],
            None if prior is None else prior["net_income"],
            reduces_profit=False,
            emph="net",
            pct_rev=_pct_of(current["net_income"], revenue),
        ),
    ]
    formulas = [quad[1], quad[2]]
    net = quad[3]

    trend_model = None
    if trend_buckets is not None:
        periods = [pname for pname, _rows in trend_buckets]
        per_period = [_is_totals(rows) for _pname, rows in trend_buckets]
        trend_model = {
            "periods": periods,
            "series": [
                {"key": "revenue", "label": "Revenue", "values": [p["revenue"] for p in per_period]},
                {"key": "gross_profit", "label": "Gross Profit", "values": [p["gross_profit"] for p in per_period]},
                {
                    "key": "operating_income",
                    "label": "Operating Income",
                    "values": [p["operating_income"] for p in per_period],
                },
                {"key": "net_income", "label": "Net Income", "values": [p["net_income"] for p in per_period]},
            ],
        }

    watch = _build_is_watch(current, prior, current_rows, prior_rows, trend_buckets)
    trend_cap_item = None
    missing_compare_arg = compare
    if trend_at_cap:
        trend_cap_item = {"tone": "warn", "text": "trend source at row cap — trend omitted"}
        # exclude "trend" so _missing_compare_watch_items doesn't ALSO add the generic
        # "Trend comparison unavailable this run" chip for the SAME underlying reason.
        missing_compare_arg = {k: v for k, v in compare.items() if k != "trend"}
    missing_items = _missing_compare_watch_items(
        missing_compare_arg, {"prior": prior_rows, "yoy": yoy_rows, "trend": trend_rows}
    )
    cap_item = _row_cap_watch_item(len(current_rows))
    priority_items = (
        ([cap_item] if cap_item is not None else [])
        + ([trend_cap_item] if trend_cap_item is not None else [])
        + missing_items
    )
    if priority_items:
        watch = (priority_items + watch)[:MAX_WATCH_ITEMS]
    highlights = _build_is_highlights(current, prior, current_rows, prior_rows)
    narrative = _build_is_narrative(current, prior, period)

    return {
        "statement": "income_statement",
        "period": period,
        "prior_period": _prior_period_label(period) if prior_rows is not None else None,
        "yoy_period": _yoy_period_label(period) if yoy_rows is not None else None,
        "kpis": kpis,
        "watch": watch,
        "trend": trend_model,
        "quad": quad,
        "sections": sections,
        "formulas": formulas,
        "net": net,
        "checks": [],
        "highlights": highlights,
        "narrative": narrative,
    }


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------


def _bs_reduces_profit(_section_key: str, _amount: Decimal) -> bool:
    # A balance sheet has no P&L "reduces profit" framing; a negative (contra) balance
    # renders via fmt_money's plain natural-sign branch, never parens. See module docstring.
    return False


def _bs_totals(rows: list[dict] | None) -> dict[str, Decimal] | None:
    if rows is None:
        return None
    assets = _section_sum(rows, "balance", "1-Assets")
    liabilities = _section_sum(rows, "balance", "2-Liabilities")
    equity = _section_sum(rows, "balance", "3-Equity")
    return {"assets": assets, "liabilities": liabilities, "equity": equity}


def _build_balance_sheet_model(section: dict, payloads: dict[str, dict]) -> dict:
    compare = section.get("compare") or {}
    period = section["period"]

    current_rows = _require_rows(payloads, section["result_id"])
    prior_rows = _resolve_rows(payloads, compare.get("prior"), amount_cols=("balance",))

    current = _bs_totals(current_rows)
    prior = _bs_totals(prior_rows)

    def get(totals, key):
        return None if totals is None else totals[key]

    kpis = [
        _kpi_row(
            "total_assets",
            "Total assets",
            current["assets"],
            get(prior, "assets"),
            None,
            margin_base=None,
            spark=None,
            neutral=True,
        ),
        _kpi_row(
            "total_liabilities",
            "Total liabilities",
            current["liabilities"],
            get(prior, "liabilities"),
            None,
            margin_base=None,
            spark=None,
            neutral=True,
        ),
        _kpi_row(
            "total_equity",
            "Total equity",
            current["equity"],
            get(prior, "equity"),
            None,
            margin_base=None,
            spark=None,
            neutral=True,
        ),
    ]

    sections = _build_sections(
        current_rows,
        prior_rows,
        amount_col="balance",
        section_order=_BS_SECTION_ORDER,
        section_labels=_BS_SECTION_LABELS,
        reduces_profit_fn=_bs_reduces_profit,
        revenue_total=None,
    )

    quad = [
        _quad_row("Total Assets", current["assets"], get(prior, "assets"), reduces_profit=False, emph="sub"),
        _quad_row(
            "Total Liabilities", current["liabilities"], get(prior, "liabilities"), reduces_profit=False, emph="sub"
        ),
        _quad_row("Total Equity", current["equity"], get(prior, "equity"), reduces_profit=False, emph="sub"),
    ]

    le_total = current["liabilities"] + current["equity"]
    balanced = current["assets"] == le_total
    diff = current["assets"] - le_total
    detail = f"Assets {fmt_money(current['assets'])} vs Liabilities + Equity {fmt_money(le_total)}"
    if not balanced:
        detail += f" (off by {fmt_money(abs(diff))})"
    checks = [{"label": "Assets = Liabilities + Equity", "ok": balanced, "detail": detail}]

    assets_mom_clause = ""
    if prior is not None:
        assets_mom_clause = f", {fmt_money_delta(current['assets'] - prior['assets'])} month-over-month"
    balance_clause = " — in balance" if balanced else " — OUT OF BALANCE, see checks"
    narrative = [
        f"Balance sheet as of {period}: total assets of {fmt_money(current['assets'])}{assets_mom_clause}.",
        (
            f"Liabilities and equity totaled {fmt_money(le_total)}, made up of {fmt_money(current['liabilities'])} in "
            f"liabilities and {fmt_money(current['equity'])} in equity{balance_clause}."
        ),
    ]

    cap_item = _row_cap_watch_item(len(current_rows))
    watch = ([cap_item] if cap_item is not None else []) + _missing_compare_watch_items(compare, {"prior": prior_rows})

    return {
        "statement": "balance_sheet",
        "period": period,
        "prior_period": _prior_period_label(period) if prior_rows is not None else None,
        "yoy_period": None,
        "kpis": kpis,
        "watch": watch,
        "trend": None,
        "quad": quad,
        "sections": sections,
        "formulas": None,
        "net": None,
        "checks": checks,
        "highlights": [],
        "narrative": narrative,
    }


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------


def _tb_reduces_profit(_section_key: str, _amount: Decimal) -> bool:
    return False


def _tb_totals(rows: list[dict] | None) -> dict[str, Decimal] | None:
    if rows is None:
        return None
    debit = sum((_to_decimal(r.get("total_debit")) for r in rows), Decimal("0"))
    credit = sum((_to_decimal(r.get("total_credit")) for r in rows), Decimal("0"))
    return {"debit": debit, "credit": credit}


def _build_trial_balance_model(section: dict, payloads: dict[str, dict]) -> dict:
    compare = section.get("compare") or {}
    period = section["period"]

    current_rows = _require_rows(payloads, section["result_id"])
    prior_rows = _resolve_rows(
        payloads, compare.get("prior"), amount_cols=("total_debit", "total_credit", "net_amount")
    )

    current = _tb_totals(current_rows)
    prior = _tb_totals(prior_rows)

    def get(totals, key):
        return None if totals is None else totals[key]

    kpis = [
        _kpi_row(
            "total_debits",
            "Total debits",
            current["debit"],
            get(prior, "debit"),
            None,
            margin_base=None,
            spark=None,
            neutral=True,
        ),
        _kpi_row(
            "total_credits",
            "Total credits",
            current["credit"],
            get(prior, "credit"),
            None,
            margin_base=None,
            spark=None,
            neutral=True,
        ),
    ]

    # TB has no ``section`` column at all -- a single flat "Accounts" group, using
    # net_amount (debit - credit) as the account row's "current" (the only single money
    # figure the account-row schema has room for; see module design notes in the task report).
    tb_section_key = "accounts"
    tb_section_order = [tb_section_key]
    tb_section_labels = {tb_section_key: "Accounts"}
    current_flat = [dict(r, section=tb_section_key) for r in current_rows]
    prior_flat = None if prior_rows is None else [dict(r, section=tb_section_key) for r in prior_rows]
    sections = _build_sections(
        current_flat,
        prior_flat,
        amount_col="net_amount",
        section_order=tb_section_order,
        section_labels=tb_section_labels,
        reduces_profit_fn=_tb_reduces_profit,
        revenue_total=None,
    )

    quad = [
        _quad_row("Total Debits", current["debit"], get(prior, "debit"), reduces_profit=False, emph="sub"),
        _quad_row("Total Credits", current["credit"], get(prior, "credit"), reduces_profit=False, emph="sub"),
    ]

    balanced = current["debit"] == current["credit"]
    diff = current["debit"] - current["credit"]
    detail = f"Total debits {fmt_money(current['debit'])} vs total credits {fmt_money(current['credit'])}"
    if not balanced:
        detail += f" (off by {fmt_money(abs(diff))})"
    checks = [{"label": "Debits = Credits", "ok": balanced, "detail": detail}]

    diff_clause = f" by {fmt_money(abs(diff))}" if not balanced else ""
    debit_str = fmt_money(current["debit"])
    credit_str = fmt_money(current["credit"])
    narrative = [
        f"Trial balance for {period}: total debits of {debit_str}, total credits of {credit_str}.",
        f"The trial balance is {'in balance' if balanced else 'out of balance'}{diff_clause}.",
    ]

    cap_item = _row_cap_watch_item(len(current_rows))
    watch = ([cap_item] if cap_item is not None else []) + _missing_compare_watch_items(compare, {"prior": prior_rows})

    return {
        "statement": "trial_balance",
        "period": period,
        "prior_period": _prior_period_label(period) if prior_rows is not None else None,
        "yoy_period": None,
        "kpis": kpis,
        "watch": watch,
        "trend": None,
        "quad": quad,
        "sections": sections,
        "formulas": None,
        "net": None,
        "checks": checks,
        "highlights": [],
        "narrative": narrative,
    }


# ---------------------------------------------------------------------------
# Period label math (self-contained — mirrors playbooks.prior_period/yoy_period without
# importing them, so this module has zero dependency on the recipe-authoring module).
# ---------------------------------------------------------------------------

_MONTH_ABBRS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _parse_period(period: str) -> tuple[int, int]:
    parts = period.strip().split(" ")
    if len(parts) != 2 or parts[1].isdigit() is False or len(parts[1]) != 4:
        raise ValueError(f"malformed period label: {period!r}")
    month_str, year_str = parts
    try:
        month = _MONTH_ABBRS.index(month_str) + 1
    except ValueError:
        raise ValueError(f"malformed period label: {period!r}") from None
    return month, int(year_str)


def _prior_period_label(period: str) -> str:
    month, year = _parse_period(period)
    if month == 1:
        return f"Dec {year - 1}"
    return f"{_MONTH_ABBRS[month - 2]} {year}"


def _yoy_period_label(period: str) -> str:
    month, year = _parse_period(period)
    return f"{_MONTH_ABBRS[month - 1]} {year - 1}"


def _parse_date_flexible(value: Any) -> tuple[int, int, int] | None:
    """(year, month, day) from a NetSuite date string in either format SuiteQL emits: ISO
    "YYYY-MM-DD" (test fixtures, some report shapes) or live "M/D/YYYY" (the format
    ``netsuite_deposit_sync._parse_date`` documents SuiteQL actually returns). ``None`` on
    anything else — this is a best-effort ORDERING fallback only, never raises."""
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(str(value), fmt)
        except ValueError:
            continue
        return (parsed.year, parsed.month, parsed.day)
    return None


def _period_sort_key(periodname: str, startdate: Any) -> tuple[int, int, int]:
    """Chronological ``(year, month, day)`` sort key for a trend bucket. Prefers the
    AUTHORITATIVE ``periodname`` ("Mon YYYY") via ``_parse_period`` — this is independent of
    whatever date-string FORMAT the source used for ``startdate``, so it can never be
    scrambled by a live-vs-fixture format mismatch (ISO "2026-06-01" sorts fine as a raw
    string; live "6/1/2026" does NOT — e.g. "10/1/2026" < "6/1/2026" lexicographically,
    putting October before June, and "1/1/2027" sorts before all twelve months of 2026).
    Falls back to parsing ``startdate`` (handles both formats) only when the period name
    itself doesn't parse; an unparseable pair sorts last, deterministically, never
    crashes."""
    try:
        month, year = _parse_period(periodname)
        return (year, month, 1)
    except ValueError:
        pass
    parsed = _parse_date_flexible(startdate)
    if parsed is not None:
        return parsed
    return (9999, 99, 99)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_BUILDERS = {
    "income_statement": _build_income_statement_model,
    "balance_sheet": _build_balance_sheet_model,
    "trial_balance": _build_trial_balance_model,
}


def build_statement_model(section: dict, payloads: dict[str, dict]) -> dict:
    statement = section.get("statement")
    builder = _BUILDERS.get(statement)
    if builder is None:
        raise ValueError(f"unknown statement type: {statement!r}")
    if not section.get("result_id"):
        raise ValueError("section is missing result_id")
    if not section.get("period"):
        raise ValueError("section is missing period")
    return builder(section, payloads)


# ---------------------------------------------------------------------------
# Task 4 (Risk 3) — JSON-persistence boundary.
#
# ``kpis[].spark`` and ``trend.series[].values`` are the model's only raw-``Decimal``
# fields (see module docstring); every other field is already a plain formatted string /
# bool / None. Standard ``json.dumps`` raises ``TypeError`` on a bare ``Decimal`` — this
# is what would crash ``db.flush()`` when a ``financial_statement`` spec is persisted to
# the ``spec_json`` JSONB column (SQLAlchemy's ``JSON`` type has no custom
# ``json_serializer`` configured; see ``app/core/database.py``).
#
# The two functions below are a pure, LOSSLESS round-trip pair, str(Decimal) <->
# Decimal(str) — never through ``float`` (no precision loss). Report-service callers use
# ``statement_model_json_safe`` to build the copy that gets persisted, AFTER rendering
# the ORIGINAL (Decimal-bearing) model — the renderer's trend tooltip
# (``report_html._fs_tip_value``) calls ``.quantize()`` and therefore requires real
# ``Decimal`` instances, so the live render always happens against the un-sanitized
# model. ``statement_model_restore_decimals`` is the inverse, for any future caller that
# re-renders from a JSON-round-tripped ``spec_json`` (today's ``/view`` endpoint always
# serves the frozen ``rendered_html`` and never re-renders from ``spec_json`` — this
# keeps that path safe if it's ever added).
# ---------------------------------------------------------------------------


def statement_model_json_safe(model: dict) -> dict:
    """A copy of a ``build_statement_model`` output with ``kpis[].spark`` and
    ``trend.series[].values`` converted from ``Decimal`` to decimal-literal strings
    (``str(Decimal)``) — safe to ``json.dumps`` without a custom encoder. Does not mutate
    ``model``; every other field passes through unchanged (already JSON-safe)."""
    out = dict(model)
    kpis = model.get("kpis")
    if kpis:
        out["kpis"] = [{**k, "spark": [str(v) for v in k["spark"]]} if k.get("spark") else dict(k) for k in kpis]
    trend = model.get("trend")
    if trend:
        out["trend"] = {
            **trend,
            "series": [{**s, "values": [str(v) for v in s.get("values") or []]} for s in trend.get("series") or []],
        }
    return out


def statement_model_restore_decimals(model: dict) -> dict:
    """Inverse of ``statement_model_json_safe``: turns ``kpis[].spark`` /
    ``trend.series[].values`` back into ``Decimal`` — the form the renderer's trend
    tooltip (``_fs_tip_value``) requires. Does not mutate ``model``."""
    out = dict(model)
    kpis = model.get("kpis")
    if kpis:
        out["kpis"] = [{**k, "spark": [Decimal(v) for v in k["spark"]]} if k.get("spark") else dict(k) for k in kpis]
    trend = model.get("trend")
    if trend:
        out["trend"] = {
            **trend,
            "series": [{**s, "values": [Decimal(v) for v in s.get("values") or []]} for s in trend.get("series") or []],
        }
    return out
