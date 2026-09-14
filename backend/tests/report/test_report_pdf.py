"""Task 4 (Slice 1) — server-side PDF rendering with WeasyPrint.

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A4. Seam: ``render_report_pdf(rendered_html: str, *, appendix_html: str | None = None)
-> bytes`` (backend/app/services/report/report_pdf.py) — WeasyPrint, no browser. The
report renderer's own ``@media print`` rules (already shipped in Task 2, see
``_IA_CSS`` / the generic ``.report`` print block in report_html.py) un-clip scroll
regions so the collapsible "full aged list" prints in full; an optional
``appendix_html`` document is rendered separately and its pages appended after the
main document's, so a caller can also hand it a standalone appendix (e.g. built from
content the main ``rendered_html`` didn't already inline).

Fixture: the SAME synthetic ``AgingReport`` fixture shape as
``test_inventory_aging_render.py`` (fake locations/SKUs/dollar values per the plan's
Global Constraints — never the mock's real tenant numbers), rendered through the real
``render_report_html`` pipeline so the PDF smoke test exercises genuine multi-section
content (KPI cards, trend chart, variance table, bucket table, largest aged positions
with a collapsible full list, highlights, narrative, sources & method) — long enough
that WeasyPrint's pagination genuinely spans >= 2 pages, which is also true after
appending a separate appendix document.

WeasyPrint needs system libraries (pango/cairo/gdk-pixbuf) this macOS dev machine does
not have installed (`.claude/rules/deploy.md` + the plan's Global Constraints: do NOT
brew-install to make it importable locally — the Docker image build is the
verification for that instead; it was run for real inside the built image as part of
this task, see the implementer's report). ``report_pdf.py`` therefore imports
``weasyprint`` LAZILY, inside ``render_report_pdf()`` itself, not at module scope —
so ``test_module_exports_render_report_pdf`` below (the genuine TDD red-before-green
test: it fails with ``ModuleNotFoundError`` until ``report_pdf.py`` exists, on ANY
machine) never touches WeasyPrint's native libraries at all. Every other test below
actually calls ``render_report_pdf`` and is skipped with a clear reason when
WeasyPrint cannot load its native libraries on the machine running pytest.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.report.inventory_aging import compute
from app.services.report.report_html import build_inventory_aging_sections, render_report_html

try:
    import weasyprint  # noqa: F401

    _WEASYPRINT_IMPORTABLE = True
    _WEASYPRINT_IMPORT_ERROR = ""
except (ImportError, OSError) as exc:  # pragma: no cover - exercised on macOS dev boxes
    _WEASYPRINT_IMPORTABLE = False
    _WEASYPRINT_IMPORT_ERROR = str(exc)

_skip_unless_weasyprint_native_libs = pytest.mark.skipif(
    not _WEASYPRINT_IMPORTABLE,
    reason=(
        "weasyprint cannot import its native pango/cairo/gdk-pixbuf libraries on this "
        f"machine (do NOT brew-install per the plan's Global Constraints — the Docker "
        f"image build is the verification instead): {_WEASYPRINT_IMPORT_ERROR}"
    ),
)


def test_module_exports_render_report_pdf():
    """The genuine TDD red-before-green test: fails with ModuleNotFoundError until
    backend/app/services/report/report_pdf.py exists, on any machine (no WeasyPrint
    native libs required — report_pdf.py must import weasyprint lazily inside the
    function, not at module scope, for this import alone to succeed)."""
    from app.services.report.report_pdf import render_report_pdf

    assert callable(render_report_pdf)


SNAPSHOT = date(2026, 9, 8)
LOCATIONS = ("Nova", "Solace", "Ember")


def _item(location, sku, days, value, qty, *, desc="Widget", category="Misc"):
    return {
        "location": location,
        "sku": sku,
        "item_desc": desc,
        "category": category,
        "qty_on_hand": qty,
        "inventory_amount": value,
        "snapshot_date": SNAPSHOT.isoformat(),
        "last_restock_date": (SNAPSHOT - timedelta(days=days)).isoformat(),
        "days": days,
        "bucket": "WRONG-ON-PURPOSE",  # compute() derives it from days, never trusts this
    }


def _prior_row(location, *, value, value_90p, value_180p, skus, skus_90p, skus_180p, qty, qty_90p):
    return {
        "location": location,
        "skus": skus,
        "qty": qty,
        "value": value,
        "skus_90p": skus_90p,
        "qty_90p": qty_90p,
        "value_90p": value_90p,
        "value_180p": value_180p,
        "skus_180p": skus_180p,
    }


def _trend_row(location, d, total_value, value_90p, pct_90p):
    return {
        "location": location,
        "d": d.isoformat(),
        "total_value": total_value,
        "value_90p": value_90p,
        "pct_90p": pct_90p,
    }


def _fixture():
    """Three synthetic locations, one of them (Ember) with 8 aged SKUs so the
    collapsible "full aged list" genuinely has more rows than the top-5 table and the
    rendered page runs long enough for real multi-page pagination."""
    items = [
        _item("Nova", "NOV-A1", 10, 50000, 100, desc="Alpha Widget", category="Widgets"),
        _item("Nova", "NOV-A2", 45, 20000, 50, desc="Beta Widget", category="Widgets"),
        _item("Nova", "NOV-A3", 75, 15000, 30, desc="Gamma Widget", category="Widgets"),
        _item("Nova", "NOV-A4", 120, 80000, 20, desc="Delta Widget", category="Widgets"),
        _item("Nova", "NOV-A5", 200, 60000, 10, desc="Epsilon Widget", category="Widgets"),
        _item("Solace", "SOL-B1", 5, 12000, 40, desc="Nu Gadget", category="Gadgets"),
        _item("Solace", "SOL-B2", 55, 9000, 30, desc="Xi Gadget", category="Gadgets"),
        _item("Solace", "SOL-B3", 140, 40000, 15, desc="Omicron Gadget", category="Gadgets"),
    ] + [_item("Ember", f"EMB-{i}", 100 + i, 10000 - i * 100, 10, desc=f"Item {i}", category="Parts") for i in range(8)]
    prior = [
        _prior_row(
            "Nova",
            value=230000,
            value_90p=70000,
            value_180p=20000,
            skus=5,
            skus_90p=2,
            skus_180p=0,
            qty=210,
            qty_90p=30,
        ),
        _prior_row(
            "Solace", value=70000, value_90p=18000, value_180p=0, skus=3, skus_90p=1, skus_180p=0, qty=90, qty_90p=15
        ),
        _prior_row(
            "Ember", value=90000, value_90p=80000, value_180p=0, skus=8, skus_90p=8, skus_180p=0, qty=80, qty_90p=80
        ),
    ]
    weekly = {
        "Nova": [(2, 240000, 80000), (1, 245000, 85000), (0, 255000, 90000)],
        "Solace": [(2, 58000, 22000), (1, 60000, 24000), (0, 61000, 40000)],
        "Ember": [(2, 88000, 78000), (1, 89000, 79000), (0, 92800, 74400)],
    }
    trend = []
    for loc, points in weekly.items():
        for weeks_ago, total_value, value_90p in points:
            d = SNAPSHOT - timedelta(weeks=weeks_ago)
            pct_90p = round(value_90p / total_value * 100, 1)
            trend.append(_trend_row(loc, d, total_value, value_90p, pct_90p))
    meta = [
        {
            "location": loc,
            "first_snapshot_date": (SNAPSHOT - timedelta(days=150)).isoformat(),
            "last_snapshot_date": SNAPSHOT.isoformat(),
            "snapshot_count": 150,
        }
        for loc in LOCATIONS
    ]
    payloads = {"r_items": items, "r_prior": prior, "r_trend": trend, "r_meta": meta}
    params = {"locations": list(LOCATIONS), "compare_days": 7, "trend_weeks": 3}
    return payloads, params


@pytest.fixture
def rendered_html():
    payloads, params = _fixture()
    report = compute(payloads, params)
    spec = {
        "title": f"Inventory Aging — Week of {report.snapshot_date.isoformat()}",
        "sections": build_inventory_aging_sections(report),
    }
    return render_report_html(spec)


def _page_count(pdf_bytes: bytes) -> int:
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return len(pdf.pages)


@_skip_unless_weasyprint_native_libs
def test_render_report_pdf_returns_pdf_bytes(rendered_html):
    from app.services.report.report_pdf import render_report_pdf

    pdf_bytes = render_report_pdf(rendered_html)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")


@_skip_unless_weasyprint_native_libs
def test_render_report_pdf_has_at_least_two_pages_for_aging_fixture(rendered_html):
    from app.services.report.report_pdf import render_report_pdf

    pdf_bytes = render_report_pdf(rendered_html)
    assert _page_count(pdf_bytes) >= 2


@_skip_unless_weasyprint_native_libs
def test_render_report_pdf_appends_appendix_pages(rendered_html):
    from app.services.report.report_pdf import render_report_pdf

    base_pdf = render_report_pdf(rendered_html)
    base_pages = _page_count(base_pdf)

    appendix_html = "<html><body>" + "".join(f"<h2>Appendix row {i}</h2>" for i in range(200)) + "</body></html>"
    combined_pdf = render_report_pdf(rendered_html, appendix_html=appendix_html)
    combined_pages = _page_count(combined_pdf)

    assert combined_pdf.startswith(b"%PDF")
    assert combined_pages > base_pages


@_skip_unless_weasyprint_native_libs
def test_render_report_pdf_ignores_script_tags():
    from app.services.report.report_pdf import render_report_pdf

    html = "<html><body><h1>Hi</h1><script>document.write('INJECTED')</script></body></html>"
    pdf_bytes = render_report_pdf(html)

    assert pdf_bytes.startswith(b"%PDF")
    assert b"INJECTED" not in pdf_bytes
    assert b"<script" not in pdf_bytes.lower()
