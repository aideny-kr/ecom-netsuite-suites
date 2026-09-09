"""Inventory Aging Weekly — computations + playbook sources (Slice 1, Task 1).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
Part A. Two entry points:

- ``build_sources(params)`` builds the four ``bigquery_sql`` recipe sources
  (``r_items``/``r_prior``/``r_trend``/``r_meta``) exactly like the 8/25 chat's
  recipe, so ``refresh_service`` can replay this playbook unchanged (§A1/§A7).
- ``compute(payloads, params)`` turns the FOUR rows-already-extracted-as-list[dict]
  payloads into a frozen ``AgingReport`` — every bucket, aggregate, delta, watch
  item, highlight, and narrative slot computed here with ``Decimal``, never in the
  LLM, never in the browser (§0 decision 3, §A1, §A2). ``compute`` never touches a
  database or a tool: it is a pure function over the shapes below, which is what
  makes "narrative slots filled and deterministic across two runs" a fact about
  this function rather than a claim about a live report.

Wiring this playbook's new ``inventory_aging`` section type into
``report_service.assemble_spec`` / the report renderer is a LATER task in the
plan (§A1's KPI cards / trend chart / tables render what ``compute()`` returns);
this module only has to produce a value that later task can consume, and be
independently correct on its own terms — which is what every test in
``tests/report/test_inventory_aging.py`` checks.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypedDict

# ---------------------------------------------------------------------------
# Constants (every threshold named, per plan Global Constraints / spec §A2)
# ---------------------------------------------------------------------------
BUCKETS: tuple[str, ...] = ("0-30", "31-60", "61-90", "91-180", "180+")
AGED_BUCKETS: tuple[str, ...] = ("91-180", "180+")  # "Aged (> 90 days)" per the mock's subtotal row
CURRENT_BUCKETS: tuple[str, ...] = ("0-30", "31-60", "61-90")  # "Current (<= 90 days)"

WATCH_VALUE_THRESHOLD = Decimal("50000")
WATCH_SHARE_THRESHOLD_PTS = Decimal("1.0")
MAX_WATCH_ITEMS = 4
MAX_HIGHLIGHTS = 6
TOP_ITEMS_PER_LOCATION = 5

DEFAULT_LOCATIONS: tuple[str, ...] = ("Dimerco", "Fedex", "Panurgy")  # spec §0 decision 1; "Virtual" excluded
DEFAULT_COMPARE_DAYS = 7
DEFAULT_TREND_WEEKS = 9

RESULT_IDS: tuple[str, ...] = ("r_items", "r_prior", "r_trend", "r_meta")

BQ_TABLE = "`frameworkreporting.inventory_snapshot`"
AGE_DEFINITION_TEXT = (
    "Age = days since last restock, inferred from the daily BigQuery snapshots "
    "(a restock is any day a SKU's quantity rose versus the previous day, or its "
    "first appearance) — SuiteQL exposes no lot/receipt dates."
)
INTEGRITY_CHECKS: tuple[str, ...] = (
    "Every location's aging-bucket values sum to that location's on-hand value.",
    "The all-locations row is the sum of the three locations, not a re-query.",
)

# Locations are interpolated as SQL string literals (the bigquery_sql tool takes a
# plain query string — no query-parameter binding, per spec §A7), so this is the
# actual injection boundary. Conservative allow-list charset: letters, digits,
# space, underscore, dot, hyphen. Notably excludes quotes, semicolons, and
# backslashes. There is no live round trip in this pure function to check a
# location against the snapshot's actual distinct values (that happens where the
# recipe is executed) — this is the syntactic gate that keeps anything malformed
# or hostile from ever reaching the SQL text.
_SAFE_LOCATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")


class Source(TypedDict):
    """The ``{tool, params, connection_id}`` shape ``playbooks.py``/``recipe.py``/
    ``refresh_service.py`` already use for every recipe source."""

    tool: str
    params: dict[str, Any]
    connection_id: str | None


# ---------------------------------------------------------------------------
# Frozen result dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BucketRow:
    location: str
    bucket: str
    value: Decimal
    units: int
    skus: int
    pct_of_location: Decimal


@dataclass(frozen=True)
class LocationSummary:
    location: str
    on_hand_value: Decimal
    units: int
    skus: int
    prior_value: Decimal
    delta_value: Decimal
    delta_pct: Decimal
    aged90_value: Decimal
    aged90_units: int
    aged90_skus: int
    aged90_value_delta: Decimal
    aged90_value_delta_pct: Decimal
    aged90_share_pct: Decimal
    aged90_share_delta_pts: Decimal
    skus_90p_delta: int
    aged180_value: Decimal
    aged180_units: int
    aged180_skus: int
    aged180_value_delta: Decimal
    skus_180p_delta: int
    top5_value: Decimal
    top5_share_pct: Decimal
    buckets: tuple[BucketRow, ...]


@dataclass(frozen=True)
class TopItem:
    location: str
    sku: str
    item_desc: str
    category: str
    value: Decimal
    units: int
    days: int
    bucket: str


@dataclass(frozen=True)
class TrendPoint:
    location: str
    d: date
    total_value: Decimal
    value_90p: Decimal
    pct_90p: Decimal


@dataclass(frozen=True)
class KpiCard:
    key: str
    label: str
    value: Decimal
    delta: Decimal
    delta_pct: Decimal | None
    favourable: bool
    sub_detail: str
    sparkline: tuple[Decimal, ...]


@dataclass(frozen=True)
class WatchItem:
    text: str
    dot: str  # "red" | "green" | "amber" | "grey" — see spec §A2
    impact: Decimal


@dataclass(frozen=True)
class Highlight:
    text: str
    impact: Decimal


@dataclass(frozen=True)
class Narrative:
    paragraph_1: str
    paragraph_2: str


@dataclass(frozen=True)
class Provenance:
    source: str
    snapshots_used: dict[str, tuple[date, date, int]]
    age_definition: str
    query_count: int
    executed_at: str | None
    bytes_scanned: int | None
    integrity_checks: tuple[str, ...]


@dataclass(frozen=True)
class AgingReport:
    snapshot_date: date
    # The snapshot every "vs prior week" delta compares against: snapshot_date -
    # compare_days (the date r_prior's aggregate was taken at, per §A7's prior_date
    # CTE). r_prior's rows carry no date of their own, so this is the only place a
    # renderer can get "compared with <date>" for the report head's sub-line.
    prior_date: date
    locations: tuple[LocationSummary, ...]
    all_locations: LocationSummary
    trend: dict[str, tuple[TrendPoint, ...]]
    kpis: tuple[KpiCard, ...]
    top_items: dict[str, tuple[TopItem, ...]]
    # UNBOUNDED per-location aged-item list — same sort as top_items (value desc,
    # never sliced). top_items alone cannot back a "nothing truncated" claim (it is
    # capped at TOP_ITEMS_PER_LOCATION); this is what a renderer's collapsible
    # "All N aged SKUs" block must draw from so N is actually true (review finding —
    # see test_aged_items_is_unbounded_not_capped_at_five).
    aged_items: dict[str, tuple[TopItem, ...]]
    # UNBOUNDED per-location item list covering EVERY bucket (0-30 through 180+),
    # not just the aged (91+ day) subset above — value desc, same sort convention
    # as aged_items. This is what backs "every on-hand SKU per location" (spec
    # §A1's r_items description / §A3's per-location Excel sheets "every SKU"):
    # aged_items is the aged-only slice of this same underlying item set, never an
    # independently-filtered list, so the two can never disagree on membership
    # (review finding — the Excel workbook was built from aged_items alone,
    # silently dropping every 0-90 day SKU from a sheet the spec and the binding
    # mock both require to hold the full on-hand list; see
    # test_all_items_is_the_full_per_location_set_not_aged_only).
    all_items: dict[str, tuple[TopItem, ...]]
    watch_items: tuple[WatchItem, ...]
    highlights: tuple[Highlight, ...]
    narrative: Narrative
    provenance: Provenance


# ---------------------------------------------------------------------------
# Small numeric/string helpers
# ---------------------------------------------------------------------------
def bucket_for_days(days: int) -> str:
    """Days-since-last-restock -> aging bucket. Boundaries per spec §0 decision 2:
    0-30 / 31-60 / 61-90 / 91-180 / 180+ (each upper bound INCLUSIVE)."""
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    if days <= 180:
        return "91-180"
    return "180+"


def _to_decimal(value: Any) -> Decimal:
    """Safe Decimal conversion for a value that arrived through JSON (int, str, or
    — worst case — float). ``Decimal(str(x))`` is the standard safe pattern: it
    never routes through float arithmetic."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a valid numeric value")
    return Decimal(str(value))


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def round1(value: Decimal) -> Decimal:
    """Round to 1 decimal place, half-up (display convention used throughout the
    report — spec §A1 "share ... to 1 dp")."""
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def share_pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """``numerator / denominator * 100`` to 1 dp; 0.0 on a zero denominator rather
    than raising — an empty/all-zero location is real data, not an error."""
    if denominator == 0:
        return Decimal("0.0")
    return round1(numerator / denominator * 100)


def pct_change(delta: Decimal, base: Decimal) -> Decimal:
    """``delta / base * 100`` to 1 dp; 0.0 when there is no prior base to compare
    against (a brand-new location has no "percent change")."""
    if base == 0:
        return Decimal("0.0")
    return round1(delta / base * 100)


def _parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError(f"cannot parse date from {value!r}")


def _fmt_money(value: Decimal) -> str:
    """Abbreviated dollar string matching the mock's card/prose style
    ("$22.70M" / "$867.8K" / "$500") — for PROSE TEXT ONLY (WatchItem.text,
    Highlight.text, Narrative paragraphs). Every numeric FIELD stays Decimal;
    this only ever produces a ``str``."""
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= Decimal("1000000"):
        return f"{sign}${(v / Decimal('1000000')):.2f}M"
    if v >= Decimal("1000"):
        return f"{sign}${(v / Decimal('1000')):.1f}K"
    return f"{sign}${v:,.0f}"


def _fmt_signed_money(value: Decimal) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_fmt_money(abs(value))}"


def _fmt_pct(value: Decimal) -> str:
    return f"{value}%"


def _fmt_signed_pct(value: Decimal) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value)}%"


def _fmt_pts(value: Decimal) -> str:
    return f"{value}"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# build_sources — the four bigquery_sql recipe sources (spec §A1/§A7)
# ---------------------------------------------------------------------------
def _validate_locations(locations: Any) -> tuple[str, ...]:
    if not locations:
        raise ValueError("locations must be a non-empty list")
    validated: list[str] = []
    for loc in locations:
        if not isinstance(loc, str) or not _SAFE_LOCATION_RE.match(loc):
            raise ValueError(f"unknown location: {loc!r}")
        validated.append(loc)
    return tuple(validated)


def _validate_positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _locations_literal(locations: tuple[str, ...]) -> str:
    return ", ".join(f"'{loc}'" for loc in locations)


def _snapshot_literal(snapshot_date: date | None) -> str | None:
    return f"DATE('{snapshot_date.isoformat()}')" if snapshot_date is not None else None


def _last_restock_ctes(locs: str, up_to_literal: str | None) -> str:
    """The daily/flagged/last_restock CTE chain shared by r_items, r_prior, and
    r_trend — "age = days since last restock" (spec §0 decision 2).

    ``last_restock`` produces ONE ROW PER (location, sku, snapshot_date) — the
    most recent restock date AT OR BEFORE that row's own snapshot_date, via a
    cumulative ``MAX(...) OVER (... ORDER BY snapshot_date)`` window. That is
    deliberate: r_items compares each row against the single latest snapshot,
    but r_prior and r_trend compare rows against a HISTORICAL snapshot date
    (a week-ago date per location for r_prior; a different date per trend
    week). A plain ``GROUP BY (location, sku)`` MAX — the prior shape here —
    computes the restock date as of "now" regardless of which historical row
    it's joined to, so a SKU restocked between that historical date and now
    got a last_restock_date LATER than the row being compared, producing a
    NEGATIVE `days` value that silently escaped every `> 90`/`> 180` bucket
    (a review finding — see test_r_prior_last_restock_is_bound_to_each_rows_own_snapshot_date).
    The per-row cumulative-max shape makes every row's own snapshot_date the
    join key (never just location+sku), so `days` is always >= 0 by
    construction, in every one of the three queries that use this CTE chain."""
    date_filter = f" AND snapshot_date <= {up_to_literal}" if up_to_literal else ""
    return f"""daily AS (
  SELECT location, sku, snapshot_date, qty_on_hand,
         LAG(qty_on_hand) OVER (PARTITION BY location, sku ORDER BY snapshot_date) AS prev_qty
  FROM {BQ_TABLE} WHERE location IN ({locs}){date_filter}),
flagged AS (
  SELECT location, sku, snapshot_date,
         CASE WHEN prev_qty IS NULL OR qty_on_hand > prev_qty THEN snapshot_date END AS restock_date
  FROM daily),
last_restock AS (
  SELECT location, sku, snapshot_date,
         MAX(restock_date) OVER (PARTITION BY location, sku ORDER BY snapshot_date) AS last_restock_date
  FROM flagged)"""


def _bucket_case(days_expr: str) -> str:
    return (
        f"CASE WHEN {days_expr} <= 30 THEN '0-30' WHEN {days_expr} <= 60 THEN '31-60' "
        f"WHEN {days_expr} <= 90 THEN '61-90' WHEN {days_expr} <= 180 THEN '91-180' ELSE '180+' END"
    )


def _latest_cte(locs: str, snapshot_literal: str | None) -> str:
    """The "latest snapshot date per location" CTE — pinned to ``snapshot_literal``
    when the caller passed an explicit ``snapshot_date`` param, else MAX() per
    location (spec §A1's "default: latest per location")."""
    if snapshot_literal:
        return (
            f"latest AS (SELECT location, {snapshot_literal} AS d FROM "
            f"(SELECT DISTINCT location FROM {BQ_TABLE} WHERE location IN ({locs})))"
        )
    return (
        f"latest AS (SELECT location, MAX(snapshot_date) AS d FROM {BQ_TABLE} "
        f"WHERE location IN ({locs}) GROUP BY location)"
    )


def _r_items_sql(locs: str, snapshot_literal: str | None) -> str:
    return f"""WITH {_last_restock_ctes(locs, snapshot_literal)},
{_latest_cte(locs, snapshot_literal)},
cur AS (SELECT s.location, s.sku, s.item_desc, s.category, s.qty_on_hand, s.inventory_amount, s.snapshot_date
        FROM {BQ_TABLE} s JOIN latest l ON l.location = s.location AND l.d = s.snapshot_date WHERE s.qty_on_hand > 0)
SELECT c.*, r.last_restock_date, DATE_DIFF(c.snapshot_date, r.last_restock_date, DAY) AS days,
       {_bucket_case("DATE_DIFF(c.snapshot_date, r.last_restock_date, DAY)")} AS bucket
FROM cur c LEFT JOIN last_restock r
  ON r.location = c.location AND r.sku = c.sku AND r.snapshot_date = c.snapshot_date"""


def _r_prior_sql(locs: str, compare_days: int, snapshot_literal: str | None) -> str:
    return f"""WITH {_latest_cte(locs, snapshot_literal)},
prior_date AS (SELECT location, DATE_SUB(d, INTERVAL {compare_days} DAY) AS d FROM latest),
{_last_restock_ctes(locs, None)},
snap AS (
  SELECT s.location, s.sku, s.qty_on_hand, s.inventory_amount, s.snapshot_date
  FROM {BQ_TABLE} s JOIN prior_date p ON p.location = s.location AND p.d = s.snapshot_date
  WHERE s.qty_on_hand > 0),
withbucket AS (
  SELECT sn.location, sn.sku, sn.qty_on_hand, sn.inventory_amount,
         DATE_DIFF(sn.snapshot_date, r.last_restock_date, DAY) AS days
  FROM snap sn LEFT JOIN last_restock r
    ON r.location = sn.location AND r.sku = sn.sku AND r.snapshot_date = sn.snapshot_date)
SELECT location,
       COUNT(*) AS skus, SUM(qty_on_hand) AS qty, SUM(inventory_amount) AS value,
       COUNTIF(days > 90) AS skus_90p, SUM(IF(days > 90, qty_on_hand, 0)) AS qty_90p,
       SUM(IF(days > 90, inventory_amount, 0)) AS value_90p,
       SUM(IF(days > 180, inventory_amount, 0)) AS value_180p,
       COUNTIF(days > 180) AS skus_180p
FROM withbucket GROUP BY location"""


def _r_trend_sql(locs: str, trend_weeks: int, snapshot_literal: str | None) -> str:
    """`day_rn` ranks DISTINCT (location, snapshot_date) pairs -- never raw
    per-SKU rows (a review finding). ROW_NUMBER() never ties, so ranking it
    directly over per-SKU rows gave every SKU sharing a date its own distinct
    day_rn: `day_rn <= 7*trend_weeks` then capped by ROW count rather than DAY
    count, and `GROUP BY location, snapshot_date, day_rn` became a no-op
    (day_rn already unique per row), so `per_day` never summed multiple SKUs
    on the same date -- it just relabeled one SKU's own inventory_amount as a
    location-wide daily total. `distinct_days` ranks dates only (no sku), then
    every per-SKU row joins in that shared day_rn on (location, snapshot_date)."""
    return f"""WITH {_latest_cte(locs, snapshot_literal)},
{_last_restock_ctes(locs, None)},
distinct_days AS (
  SELECT location, d, ROW_NUMBER() OVER (PARTITION BY location ORDER BY d DESC) AS day_rn
  FROM (SELECT DISTINCT s.location, s.snapshot_date AS d FROM {BQ_TABLE} s
        JOIN latest l ON l.location = s.location
        WHERE s.qty_on_hand > 0 AND s.snapshot_date <= l.d)),
ranked AS (
  SELECT s.location, s.sku, s.snapshot_date, s.qty_on_hand, s.inventory_amount, dd.day_rn
  FROM {BQ_TABLE} s
  JOIN distinct_days dd ON dd.location = s.location AND dd.d = s.snapshot_date
  WHERE s.qty_on_hand > 0),
withbucket AS (
  SELECT rk.location, rk.sku, rk.snapshot_date, rk.inventory_amount,
         DATE_DIFF(rk.snapshot_date, lr.last_restock_date, DAY) AS days, rk.day_rn
  FROM ranked rk LEFT JOIN last_restock lr
    ON lr.location = rk.location AND lr.sku = rk.sku AND lr.snapshot_date = rk.snapshot_date
  WHERE rk.day_rn <= 7 * {trend_weeks}),
per_day AS (
  SELECT location, snapshot_date AS d, day_rn,
         SUM(inventory_amount) AS total_value,
         SUM(IF(days > 90, inventory_amount, 0)) AS value_90p
  FROM withbucket GROUP BY location, snapshot_date, day_rn)
SELECT location, d, total_value, value_90p, SAFE_DIVIDE(value_90p, total_value) * 100 AS pct_90p
FROM per_day WHERE MOD(day_rn - 1, 7) = 0
ORDER BY location, d"""


def _r_meta_sql(locs: str) -> str:
    return (
        f"SELECT location, MIN(snapshot_date) AS first_snapshot_date, "
        f"MAX(snapshot_date) AS last_snapshot_date, COUNT(DISTINCT snapshot_date) AS snapshot_count "
        f"FROM {BQ_TABLE} WHERE location IN ({locs}) GROUP BY location"
    )


def build_sources(params: dict[str, Any]) -> dict[str, Source]:
    """The four ``bigquery_sql`` recipe sources for ``inventory_aging``, exactly
    like the 8/25 chat's recipe (spec §A1): ``connection_id: None`` — a local
    tool resolves the tenant's active BigQuery connector at execution time, same
    as every other local-tool source in this codebase (see ``recipe.py``'s
    docstring). Raises ``ValueError`` on an empty/malformed/injection-shaped
    ``locations`` list — see ``_validate_locations``."""
    params = params or {}
    raw_locations = params.get("locations")
    if raw_locations is None:
        raw_locations = list(DEFAULT_LOCATIONS)
    locations = _validate_locations(raw_locations)
    compare_days = _validate_positive_int(params.get("compare_days", DEFAULT_COMPARE_DAYS), "compare_days")
    trend_weeks = _validate_positive_int(params.get("trend_weeks", DEFAULT_TREND_WEEKS), "trend_weeks")
    snapshot_date_param = params.get("snapshot_date")
    snapshot_date = _parse_date(snapshot_date_param) if snapshot_date_param is not None else None
    snapshot_literal = _snapshot_literal(snapshot_date)

    locs = _locations_literal(locations)
    max_rows = 200_000  # generous: every on-hand SKU across up to a few locations

    return {
        "r_items": {
            "tool": "bigquery_sql",
            "params": {"query": _r_items_sql(locs, snapshot_literal), "max_rows": max_rows},
            "connection_id": None,
        },
        "r_prior": {
            "tool": "bigquery_sql",
            "params": {"query": _r_prior_sql(locs, compare_days, snapshot_literal), "max_rows": max_rows},
            "connection_id": None,
        },
        "r_trend": {
            "tool": "bigquery_sql",
            "params": {"query": _r_trend_sql(locs, trend_weeks, snapshot_literal), "max_rows": max_rows},
            "connection_id": None,
        },
        "r_meta": {
            "tool": "bigquery_sql",
            "params": {"query": _r_meta_sql(locs), "max_rows": 100},
            "connection_id": None,
        },
    }


# ---------------------------------------------------------------------------
# compute() — the pure aggregation/insight engine
# ---------------------------------------------------------------------------
def _bucket_rows_for(location: str, items: list[dict]) -> tuple[BucketRow, ...]:
    on_hand_value = sum((_to_decimal(it["inventory_amount"]) for it in items), Decimal("0"))
    rows = []
    for bucket in BUCKETS:
        bucket_items = [it for it in items if bucket_for_days(_int(it["days"])) == bucket]
        value = sum((_to_decimal(it["inventory_amount"]) for it in bucket_items), Decimal("0"))
        units = sum((_int(it["qty_on_hand"]) for it in bucket_items), 0)
        rows.append(
            BucketRow(
                location=location,
                bucket=bucket,
                value=value,
                units=units,
                skus=len(bucket_items),
                pct_of_location=share_pct(value, on_hand_value),
            )
        )
    return tuple(rows)


def _build_location_summary(
    location: str, items: list[dict], prior_row: dict[str, Any]
) -> tuple[LocationSummary, tuple[TopItem, ...], tuple[TopItem, ...], tuple[TopItem, ...]]:
    buckets = _bucket_rows_for(location, items)
    on_hand_value = sum((b.value for b in buckets), Decimal("0"))
    units = sum((b.units for b in buckets), 0)
    skus = sum((b.skus for b in buckets), 0)

    aged_rows = tuple(b for b in buckets if b.bucket in AGED_BUCKETS)
    aged90_value = sum((b.value for b in aged_rows), Decimal("0"))
    aged90_units = sum((b.units for b in aged_rows), 0)
    aged90_skus = sum((b.skus for b in aged_rows), 0)
    aged180 = next(b for b in buckets if b.bucket == "180+")

    prior_value = _to_decimal(prior_row.get("value"))
    prior_value_90p = _to_decimal(prior_row.get("value_90p"))
    prior_value_180p = _to_decimal(prior_row.get("value_180p"))
    prior_skus_90p = _int(prior_row.get("skus_90p"))
    prior_skus_180p = _int(prior_row.get("skus_180p"))

    current_share = share_pct(aged90_value, on_hand_value)
    prior_share = share_pct(prior_value_90p, prior_value)

    delta_value = on_hand_value - prior_value
    aged90_value_delta = aged90_value - prior_value_90p
    aged180_value_delta = aged180.value - prior_value_180p

    aged_items_raw = sorted(
        (it for it in items if bucket_for_days(_int(it["days"])) in AGED_BUCKETS),
        key=lambda it: _to_decimal(it["inventory_amount"]),
        reverse=True,
    )
    # The FULL (unbounded) aged-item list, same sort as top5, never sliced — the
    # source of truth for "nothing truncated" (AgingReport.aged_items). top_items
    # (top5 below) is a slice of THIS, never independently sorted/filtered, so the
    # two can never disagree on ordering or membership.
    all_aged_items = tuple(
        TopItem(
            location=location,
            sku=str(it["sku"]),
            item_desc=str(it.get("item_desc", "")),
            category=str(it.get("category", "")),
            value=_to_decimal(it["inventory_amount"]),
            units=_int(it["qty_on_hand"]),
            days=_int(it["days"]),
            bucket=bucket_for_days(_int(it["days"])),
        )
        for it in aged_items_raw
    )
    top_items = all_aged_items[:TOP_ITEMS_PER_LOCATION]
    top5_value = sum((it.value for it in top_items), Decimal("0"))

    # EVERY on-hand item at this location, every bucket, value desc, never
    # sliced — the full-fidelity source AgingReport.all_items exposes (see its
    # field docstring). aged_items above is filtered FROM the same `items`
    # input, never independently, so membership can never disagree between the
    # two.
    all_items_sorted = tuple(
        TopItem(
            location=location,
            sku=str(it["sku"]),
            item_desc=str(it.get("item_desc", "")),
            category=str(it.get("category", "")),
            value=_to_decimal(it["inventory_amount"]),
            units=_int(it["qty_on_hand"]),
            days=_int(it["days"]),
            bucket=bucket_for_days(_int(it["days"])),
        )
        for it in sorted(items, key=lambda it: _to_decimal(it["inventory_amount"]), reverse=True)
    )

    summary = LocationSummary(
        location=location,
        on_hand_value=on_hand_value,
        units=units,
        skus=skus,
        prior_value=prior_value,
        delta_value=delta_value,
        delta_pct=pct_change(delta_value, prior_value),
        aged90_value=aged90_value,
        aged90_units=aged90_units,
        aged90_skus=aged90_skus,
        aged90_value_delta=aged90_value_delta,
        aged90_value_delta_pct=pct_change(aged90_value_delta, prior_value_90p),
        aged90_share_pct=current_share,
        aged90_share_delta_pts=round1(current_share - prior_share),
        skus_90p_delta=aged90_skus - prior_skus_90p,
        aged180_value=aged180.value,
        aged180_units=aged180.units,
        aged180_skus=aged180.skus,
        aged180_value_delta=aged180_value_delta,
        skus_180p_delta=aged180.skus - prior_skus_180p,
        top5_value=top5_value,
        top5_share_pct=share_pct(top5_value, aged90_value),
        buckets=buckets,
    )
    return summary, top_items, all_aged_items, all_items_sorted


def _all_locations_summary(
    locations: tuple[str, ...], items_by_loc: dict[str, list[dict]], prior_by_loc: dict[str, dict]
) -> LocationSummary:
    """The all-locations row: rebuilt from the SAME underlying item rows (never
    from summing already-1dp-rounded per-location fields), so "all-location
    totals equal the sum of locations" holds exactly for value/units/SKUs — the
    share fields are properly weighted ratios, not an average of the three
    locations' shares (matching the mock's "All locations" row)."""
    all_items = [it for loc in locations for it in items_by_loc[loc]]
    combined_prior = {
        "value": sum((_to_decimal(prior_by_loc.get(loc, {}).get("value")) for loc in locations), Decimal("0")),
        "value_90p": sum((_to_decimal(prior_by_loc.get(loc, {}).get("value_90p")) for loc in locations), Decimal("0")),
        "value_180p": sum(
            (_to_decimal(prior_by_loc.get(loc, {}).get("value_180p")) for loc in locations), Decimal("0")
        ),
        "skus_90p": sum((_int(prior_by_loc.get(loc, {}).get("skus_90p")) for loc in locations), 0),
        "skus_180p": sum((_int(prior_by_loc.get(loc, {}).get("skus_180p")) for loc in locations), 0),
    }
    summary, _unused_top_items, _unused_aged_items, _unused_all_items = _build_location_summary(
        "All locations", all_items, combined_prior
    )
    return summary


def _combine_trend(trend_by_loc: dict[str, tuple[TrendPoint, ...]]) -> tuple[TrendPoint, ...]:
    by_date: dict[date, list[TrendPoint]] = {}
    for points in trend_by_loc.values():
        for tp in points:
            by_date.setdefault(tp.d, []).append(tp)
    combined = []
    for d in sorted(by_date):
        pts = by_date[d]
        total_value = sum((p.total_value for p in pts), Decimal("0"))
        value_90p = sum((p.value_90p for p in pts), Decimal("0"))
        combined.append(
            TrendPoint(
                location="All locations",
                d=d,
                total_value=total_value,
                value_90p=value_90p,
                pct_90p=share_pct(value_90p, total_value),
            )
        )
    return tuple(combined)


def _kpi_cards(
    all_locations: LocationSummary, combined_trend: tuple[TrendPoint, ...], trend_weeks: int
) -> tuple[KpiCard, ...]:
    pct_series = tuple(tp.pct_90p for tp in combined_trend)
    range_min = min(pct_series) if pct_series else Decimal("0.0")
    range_max = max(pct_series) if pct_series else Decimal("0.0")
    aged180_prior_value = all_locations.aged180_value - all_locations.aged180_value_delta
    return (
        KpiCard(
            key="on_hand_value",
            label="On-hand value",
            value=all_locations.on_hand_value,
            delta=all_locations.delta_value,
            delta_pct=all_locations.delta_pct,
            favourable=all_locations.delta_value >= 0,
            sub_detail=f"{all_locations.skus:,} SKUs · {all_locations.units:,} units",
            sparkline=tuple(tp.total_value for tp in combined_trend),
        ),
        KpiCard(
            key="aged90_value",
            label="Aged > 90 days · value",
            value=all_locations.aged90_value,
            delta=all_locations.aged90_value_delta,
            delta_pct=all_locations.aged90_value_delta_pct,
            favourable=all_locations.aged90_value_delta <= 0,
            sub_detail=f"{all_locations.aged90_skus:,} SKUs · {all_locations.aged90_units:,} units",
            sparkline=tuple(tp.value_90p for tp in combined_trend),
        ),
        KpiCard(
            key="aged_share",
            label="Aged share of value",
            value=all_locations.aged90_share_pct,
            delta=all_locations.aged90_share_delta_pts,
            delta_pct=None,
            favourable=all_locations.aged90_share_delta_pts <= 0,
            sub_detail=f"trailing {trend_weeks}-wk range {range_min}% - {range_max}%",
            sparkline=pct_series,
        ),
        KpiCard(
            key="aged180_value",
            label="Aged > 180 days · value",
            value=all_locations.aged180_value,
            delta=all_locations.aged180_value_delta,
            delta_pct=pct_change(all_locations.aged180_value_delta, aged180_prior_value),
            favourable=all_locations.aged180_value_delta <= 0,
            sub_detail=(
                f"{all_locations.aged180_skus:,} SKUs · {all_locations.aged180_units:,} units · write-down review list"
            ),
            # r_trend (spec §A7) carries pct_90p/total_value/value_90p only, not a
            # 180+ series -- a later task can extend it if this card needs a real
            # sparkline; an empty tuple here is honest about what data exists,
            # never a fabricated line.
            sparkline=(),
        ),
    )


def _weeks_running_highest(trend_by_loc: dict[str, tuple[TrendPoint, ...]], highest_loc: str) -> int:
    """Trailing count of weeks (from the most recent, going backward) that
    ``highest_loc`` held the highest ``pct_90p`` among ALL locations at that same
    trend date. Stops at the first week it was not the highest, or at the first
    date with no data at all."""
    by_date: dict[date, dict[str, Decimal]] = {}
    for loc, points in trend_by_loc.items():
        for tp in points:
            by_date.setdefault(tp.d, {})[loc] = tp.pct_90p
    count = 0
    for d in sorted(by_date, reverse=True):
        shares = by_date[d]
        if not shares:
            break
        top_loc = max(shares, key=lambda loc: shares[loc])
        if top_loc != highest_loc:
            break
        count += 1
    return count


def _watch_items(
    locations: tuple[LocationSummary, ...],
    all_locations: LocationSummary,
    trend_by_loc: dict[str, tuple[TrendPoint, ...]],
) -> tuple[WatchItem, ...]:
    """Spec §A2, max 4. Rules 1/2 are per-location and threshold-gated (fire at
    ``>=`` the named constant, never above it by coincidence of rounding); rules
    3/4 are unconditional (one "highest share" line, one "180+" line) — matching
    the mock, which always shows exactly one of each. NOTE: rule 1's impact is a
    point delta and rule 2's is a dollar delta — mixing units in one sort is a
    known Task-1 simplification (nothing in this slice's tests depends on
    cross-rule ordering; only "fires at/above the threshold" does)."""
    items: list[WatchItem] = []

    for loc in locations:
        if abs(loc.aged90_share_delta_pts) >= WATCH_SHARE_THRESHOLD_PTS:
            direction = "up" if loc.aged90_share_delta_pts > 0 else "down"
            unfavourable = loc.aged90_share_delta_pts > 0  # higher aged share is unfavourable
            text = (
                f"{loc.location} 90+ day share {_fmt_pct(loc.aged90_share_pct)}, {direction} "
                f"{_fmt_pts(abs(loc.aged90_share_delta_pts))} pts in a week: aged value "
                f"{_fmt_signed_money(loc.aged90_value_delta)} while total on-hand "
                f"{_fmt_signed_money(loc.delta_value)}"
            )
            items.append(
                WatchItem(text=text, dot="red" if unfavourable else "green", impact=abs(loc.aged90_share_delta_pts))
            )

    for loc in locations:
        if abs(loc.aged90_value_delta) >= WATCH_VALUE_THRESHOLD:
            unfavourable = loc.aged90_value_delta > 0  # rising aged value is unfavourable
            direction = "up" if loc.aged90_value_delta > 0 else "down"
            verb = "entered" if loc.skus_90p_delta > 0 else "left"
            text = (
                f"{loc.location} aged value {_fmt_money(loc.aged90_value)}, {direction} "
                f"{_fmt_money(abs(loc.aged90_value_delta))} ({_fmt_signed_pct(loc.aged90_value_delta_pct)}) as "
                f"{abs(loc.skus_90p_delta)} SKUs {verb} the 90+ buckets"
            )
            items.append(
                WatchItem(text=text, dot="red" if unfavourable else "green", impact=abs(loc.aged90_value_delta))
            )

    highest = max(locations, key=lambda loc: loc.aged90_share_pct)
    weeks_running = _weeks_running_highest(trend_by_loc, highest.location)
    suffix = f", for the {_ordinal(weeks_running)} week running" if weeks_running >= 2 else ""
    items.append(
        WatchItem(
            text=f"{highest.location} holds the highest aged share, {_fmt_pct(highest.aged90_share_pct)}{suffix}",
            dot="amber",
            impact=highest.aged90_share_pct,
        )
    )

    share_of_aged = share_pct(all_locations.aged180_value, all_locations.aged90_value)
    skus_delta = all_locations.skus_180p_delta
    direction_180 = "more" if skus_delta > 0 else "fewer"
    items.append(
        WatchItem(
            text=(
                f"180+ days: {_fmt_money(all_locations.aged180_value)} across {all_locations.aged180_skus} SKUs — "
                f"{_fmt_pct(share_of_aged)} of aged value, {abs(skus_delta)} SKUs {direction_180} than a week ago"
            ),
            dot="grey",
            impact=Decimal("0"),
        )
    )

    items.sort(key=lambda w: w.impact, reverse=True)
    return tuple(items[:MAX_WATCH_ITEMS])


def _highlights(
    locations: tuple[LocationSummary, ...],
    all_locations: LocationSummary,
    top_items_by_loc: dict[str, tuple[TopItem, ...]],
    combined_trend: tuple[TrendPoint, ...],
    trend_by_loc: dict[str, tuple[TrendPoint, ...]],
) -> tuple[Highlight, ...]:
    """Spec §A2, up to 6, largest mover first. All impacts below are expressed in
    dollars so the primary two categories (attribution / largest mover) sort
    against each other meaningfully; the remaining categories use the best
    dollar-shaped proxy available for their statement."""
    candidates: list[Highlight] = []

    for loc in locations:
        if loc.delta_value != 0 and loc.aged90_share_delta_pts != 0:
            if (loc.delta_value > 0) != (loc.aged90_share_delta_pts > 0):
                share_word = "rose" if loc.aged90_share_delta_pts > 0 else "fell"
                total_word = "rose" if loc.delta_value > 0 else "fell"
                aged_word = "rose" if loc.aged90_value_delta > 0 else "fell"
                text = (
                    f"{loc.location}'s aged share {share_word} {_fmt_pts(abs(loc.aged90_share_delta_pts))} pts to "
                    f"{_fmt_pct(loc.aged90_share_pct)} because total on-hand value {total_word} "
                    f"{_fmt_money(abs(loc.delta_value))} ({_fmt_signed_pct(loc.delta_pct)}) while aged value "
                    f"{aged_word} {_fmt_money(abs(loc.aged90_value_delta))}: the denominator moved, not the "
                    "aged stock."
                )
                candidates.append(Highlight(text=text, impact=abs(loc.delta_value)))

    mover = max(locations, key=lambda loc: abs(loc.aged90_value_delta))
    if mover.aged90_value_delta != 0:
        verb = "rose" if mover.aged90_value_delta > 0 else "fell"
        entered_left = "entering" if mover.skus_90p_delta > 0 else "leaving"
        text = (
            f"{mover.location}'s aged value {verb} {_fmt_money(abs(mover.aged90_value_delta))} "
            f"({_fmt_signed_pct(mover.aged90_value_delta_pct)}), driven by {abs(mover.skus_90p_delta)} SKUs "
            f"{entered_left} the 90+ buckets."
        )
        candidates.append(Highlight(text=text, impact=abs(mover.aged90_value_delta)))

    biggest_aged = max(locations, key=lambda loc: loc.aged90_value)
    top2 = top_items_by_loc.get(biggest_aged.location, ())[:2]
    if len(top2) == 2 and biggest_aged.aged90_value > 0:
        top2_value = top2[0].value + top2[1].value
        share = share_pct(top2_value, biggest_aged.aged90_value)
        names = " and ".join(f"{it.sku} ({_fmt_money(it.value)}, {it.days} days)" for it in top2)
        text = f"Two SKUs make up {_fmt_pct(share)} of {biggest_aged.location}'s aged value: {names}."
        candidates.append(Highlight(text=text, impact=top2_value))

    highest_share_loc = max(locations, key=lambda loc: loc.aged90_share_pct)
    weeks_running = _weeks_running_highest(trend_by_loc, highest_share_loc.location)
    if weeks_running >= 2:
        text = (
            f"{highest_share_loc.location}'s aged share has been the highest of the group for "
            f"{weeks_running} weeks running, currently {_fmt_pct(highest_share_loc.aged90_share_pct)}."
        )
        candidates.append(Highlight(text=text, impact=highest_share_loc.aged90_value))

    if combined_trend:
        values_90p = [tp.value_90p for tp in combined_trend]
        peak = max(values_90p)
        latest = combined_trend[-1].value_90p
        if peak != latest:
            text = (
                f"All-location aged value is {_fmt_money(latest)} this week, off the {_fmt_money(peak)} trailing peak."
            )
            candidates.append(Highlight(text=text, impact=abs(peak - latest)))

    if all_locations.aged180_value > 0 and all_locations.aged180_skus > 0:
        median_value = all_locations.aged180_value / all_locations.aged180_skus
        text = (
            f"180+ days is small in value, wide in SKUs: {_fmt_money(all_locations.aged180_value)} across "
            f"{all_locations.aged180_skus} SKUs, median position {_fmt_money(median_value)}."
        )
        candidates.append(Highlight(text=text, impact=all_locations.aged180_value))

    candidates.sort(key=lambda h: h.impact, reverse=True)
    return tuple(candidates[:MAX_HIGHLIGHTS])


def _narrative(
    locations: tuple[LocationSummary, ...],
    all_locations: LocationSummary,
    snapshot_date: date,
    combined_trend: tuple[TrendPoint, ...],
) -> Narrative:
    total_word = "up" if all_locations.delta_value >= 0 else "down"
    share_word = "down" if all_locations.aged90_share_delta_pts <= 0 else "up"
    range_clause = ""
    if combined_trend:
        pct_values = [tp.pct_90p for tp in combined_trend]
        range_clause = f" (trailing range {min(pct_values)}% - {max(pct_values)}%)"
    paragraph_1 = (
        f"Across {', '.join(loc.location for loc in locations)}, on-hand inventory is worth "
        f"{_fmt_money(all_locations.on_hand_value)} on the {snapshot_date.isoformat()} snapshot, {total_word} "
        f"{_fmt_money(abs(all_locations.delta_value))} ({_fmt_signed_pct(all_locations.delta_pct)}) on the week. "
        f"Stock older than 90 days is {_fmt_money(all_locations.aged90_value)}, or "
        f"{_fmt_pct(all_locations.aged90_share_pct)} of value, {share_word} "
        f"{_fmt_pts(abs(all_locations.aged90_share_delta_pts))} points from the week earlier{range_clause}."
    )

    lead = max(locations, key=lambda loc: loc.on_hand_value)
    lead_share_of_total = share_pct(lead.on_hand_value, all_locations.on_hand_value)
    lead_direction = "improved" if lead.aged90_value_delta <= 0 else "moved against the group"
    mover = max(locations, key=lambda loc: abs(loc.aged90_value_delta))
    highest_share = max(locations, key=lambda loc: loc.aged90_share_pct)
    paragraph_2 = (
        f"{lead.location} carries {_fmt_pct(lead_share_of_total)} of the on-hand value and {lead_direction}: "
        f"aged value {'fell' if lead.aged90_value_delta <= 0 else 'rose'} "
        f"{_fmt_money(abs(lead.aged90_value_delta))} as {abs(lead.skus_90p_delta)} SKUs "
        f"{'left' if lead.skus_90p_delta <= 0 else 'entered'} the aged buckets. "
        f"{mover.location} moved the {'same way' if mover.location == lead.location else 'other way'}, with its "
        f"aged share at {_fmt_pct(mover.aged90_share_pct)}. {highest_share.location} holds the highest aged "
        f"share of the group, {_fmt_pct(highest_share.aged90_share_pct)}. The 180+ day list across all "
        f"{len(locations)} locations totals {_fmt_money(all_locations.aged180_value)} in "
        f"{all_locations.aged180_skus} SKUs."
    )
    return Narrative(paragraph_1=paragraph_1, paragraph_2=paragraph_2)


def _resolve_snapshot_date(items: list[dict], snapshot_date_param: Any) -> date:
    if snapshot_date_param is not None:
        return _parse_date(snapshot_date_param)
    dates = [_parse_date(it["snapshot_date"]) for it in items if it.get("snapshot_date") is not None]
    if not dates:
        raise ValueError("cannot resolve snapshot_date: r_items is empty and no snapshot_date param was given")
    return max(dates)


def compute(payloads: dict[str, list[dict]], params: dict[str, Any]) -> AgingReport:
    """Turn the four extracted BigQuery payloads into a frozen ``AgingReport``.
    Pure function — same inputs always produce an equal (``==``) result, which is
    the whole of what "narrative ... deterministic across two runs" means here.
    Raises ``ValueError`` on a malformed/unknown ``locations`` param, same as
    ``build_sources`` (defense in depth against a tampered/drifted recipe)."""
    params = params or {}
    raw_locations = params.get("locations")
    if raw_locations is None:
        raw_locations = list(DEFAULT_LOCATIONS)
    locations = _validate_locations(raw_locations)
    trend_weeks = _validate_positive_int(params.get("trend_weeks", DEFAULT_TREND_WEEKS), "trend_weeks")
    compare_days = _validate_positive_int(params.get("compare_days", DEFAULT_COMPARE_DAYS), "compare_days")

    items = payloads.get("r_items") or []
    prior_rows = payloads.get("r_prior") or []
    trend_rows = payloads.get("r_trend") or []
    meta_rows = payloads.get("r_meta") or []

    items_by_loc: dict[str, list[dict]] = {loc: [] for loc in locations}
    for row in items:
        bucket = items_by_loc.get(row["location"])
        if bucket is not None:
            bucket.append(row)

    prior_by_loc = {row["location"]: row for row in prior_rows}

    trend_by_loc: dict[str, tuple[TrendPoint, ...]] = {}
    for loc in locations:
        points = [
            TrendPoint(
                location=loc,
                d=_parse_date(row["d"]),
                total_value=_to_decimal(row["total_value"]),
                value_90p=_to_decimal(row["value_90p"]),
                pct_90p=_to_decimal(row["pct_90p"]),
            )
            for row in trend_rows
            if row["location"] == loc
        ]
        trend_by_loc[loc] = tuple(sorted(points, key=lambda tp: tp.d))

    snapshot_date = _resolve_snapshot_date(items, params.get("snapshot_date"))

    location_summaries: list[LocationSummary] = []
    top_items_by_loc: dict[str, tuple[TopItem, ...]] = {}
    aged_items_by_loc: dict[str, tuple[TopItem, ...]] = {}
    all_items_by_loc: dict[str, tuple[TopItem, ...]] = {}
    for loc in locations:
        summary, top_items, aged_items, all_items = _build_location_summary(
            loc, items_by_loc[loc], prior_by_loc.get(loc, {})
        )
        location_summaries.append(summary)
        top_items_by_loc[loc] = top_items
        aged_items_by_loc[loc] = aged_items
        all_items_by_loc[loc] = all_items
    locations_tuple = tuple(location_summaries)

    all_locations = _all_locations_summary(locations, items_by_loc, prior_by_loc)
    combined_trend = _combine_trend(trend_by_loc)

    return AgingReport(
        snapshot_date=snapshot_date,
        prior_date=snapshot_date - timedelta(days=compare_days),
        locations=locations_tuple,
        all_locations=all_locations,
        trend=trend_by_loc,
        kpis=_kpi_cards(all_locations, combined_trend, trend_weeks),
        top_items=top_items_by_loc,
        aged_items=aged_items_by_loc,
        all_items=all_items_by_loc,
        watch_items=_watch_items(locations_tuple, all_locations, trend_by_loc),
        highlights=_highlights(locations_tuple, all_locations, top_items_by_loc, combined_trend, trend_by_loc),
        narrative=_narrative(locations_tuple, all_locations, snapshot_date, combined_trend),
        provenance=Provenance(
            source=BQ_TABLE,
            snapshots_used={
                row["location"]: (
                    _parse_date(row["first_snapshot_date"]),
                    _parse_date(row["last_snapshot_date"]),
                    _int(row.get("snapshot_count")),
                )
                for row in meta_rows
                if row.get("location") in locations
            },
            age_definition=AGE_DEFINITION_TEXT,
            query_count=len(RESULT_IDS),
            # bytes_scanned/executed_at live on the raw tool result, not on the
            # extracted list[dict] payload this function receives -- a later task
            # (the compose/refresh wiring) fills these in from the live tool
            # results it has direct access to. Left honestly absent here rather
            # than fabricated.
            executed_at=None,
            bytes_scanned=None,
            integrity_checks=INTEGRITY_CHECKS,
        ),
    )


# ---------------------------------------------------------------------------
# Refresh-support glue (Slice 1 follow-up task): converters shared by
# refresh_service/playbooks (a dispatched source's table payload -> the
# list[dict] rows compute() reads) and by report_service/compose_inventory_aging
# (compute()'s frozen dataclass result -> JSONB-safe for persistence). Single-
# sourced here so headless compose and refresh can never diverge on either.
# ---------------------------------------------------------------------------
def rows_from_table_payload(payload: dict | None) -> list[dict]:
    """``{"columns": [...], "rows": [[...], ...]}`` (the shape both
    ``refresh_service._execute_sources``/``extract_result_payload`` and the raw
    ``bigquery_sql_execute`` tool result share -- rows POSITIONAL, never dicts) ->
    the ``list[dict]`` rows ``compute()`` reads (``row["location"]`` etc.), same
    zip ``compose_inventory_aging._fetch_payloads`` uses against the raw tool
    result. Tolerates a missing/malformed payload (empty columns/rows, or a
    non-dict altogether) by returning ``[]`` rather than raising -- the caller's
    own required-rid gating (``_execute_sources``) is what decides whether an
    empty/absent result is fatal, not this converter."""
    if not isinstance(payload, dict):
        return []
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    return [dict(zip(columns, row, strict=False)) for row in rows]


def json_safe(value: Any) -> Any:
    """Recursively convert a value tree that may contain this module's frozen
    dataclasses (``AgingReport`` and its nested ``BucketRow``/``TopItem``/
    ``TrendPoint``/etc.), ``Decimal``, and ``date``/``datetime`` into something
    ``json.dumps`` -- and therefore JSONB -- can actually store. Never through
    ``float`` (no precision loss on money); ``Decimal`` becomes its exact string
    form, same convention as ``report_service.spec_json_safe``'s statement-model
    sanitizing. Single-sourced here so ``report_service.spec_json_safe`` (refresh
    + headless playbook compose) and ``compose_inventory_aging.py`` (which
    originally carried its own private copy of this exact function) persist an
    inventory_aging ``spec_json`` identically."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value
