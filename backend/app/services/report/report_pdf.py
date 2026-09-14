"""Server-side PDF rendering for a rendered report page.

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§A4. Turns the same self-contained HTML string ``render_report_html`` already
produces (inline SVG, no external network fetches, the report's own ``@media print``
CSS block un-clips scroll regions so the collapsible "full aged list" prints in full)
into PDF bytes via WeasyPrint — no headless browser.

``weasyprint`` is imported LAZILY, inside ``render_report_pdf()``, not at module
scope. WeasyPrint loads its native pango/cairo/gdk-pixbuf libraries via ``cffi``
the moment it is imported, which raises ``OSError`` on any machine missing those
system libraries (this repo's dev machines are not guaranteed to have them — the
Dockerfile installs them for the deployed image, see ``.claude/rules/deploy.md``).
A lazy import keeps this module importable everywhere (so callers, and this
module's own "does the function exist" test, never need WeasyPrint's native libs
just to be collected/typed against) and turns that OSError into the same
``ReportPdfUnavailableError`` a caller with no system libs would otherwise get as a
much less legible `cffi` traceback.
"""

from __future__ import annotations


class ReportPdfUnavailableError(RuntimeError):
    """Raised when WeasyPrint cannot load its native rendering libraries."""


def render_report_pdf(rendered_html: str, *, appendix_html: str | None = None) -> bytes:
    """Render ``rendered_html`` (and, if given, a separate ``appendix_html``
    document appended as its own trailing pages) to PDF bytes.

    Both documents are rendered independently and their pages concatenated —
    this lets a caller hand ``appendix_html`` content that was never part of the
    main ``rendered_html`` string at all (e.g. a standalone appendix built from a
    different template), while a caller whose "full aged list" is already inlined
    in ``rendered_html`` (as this report's collapsible ``<details>`` block is, see
    Task 2's ``build_inventory_aging_sections``) can simply omit ``appendix_html``.

    WeasyPrint never executes ``<script>`` content and does not fetch external
    resources by default — passed HTML is treated as a static document, matching
    the report renderer's own "no <script> tags" design rule.
    """
    try:
        from weasyprint import HTML
    except OSError as exc:  # pragma: no cover - exercised only where native libs are missing
        raise ReportPdfUnavailableError(
            "WeasyPrint could not load its native pango/cairo/gdk-pixbuf libraries "
            "(see .claude/rules/deploy.md and the Dockerfile's WeasyPrint system "
            f"packages): {exc}"
        ) from exc

    documents = [HTML(string=rendered_html).render()]
    if appendix_html:
        documents.append(HTML(string=appendix_html).render())

    pages = [page for document in documents for page in document.pages]
    return documents[0].copy(pages).write_pdf()
