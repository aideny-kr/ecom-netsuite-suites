# Synthetic frozen report fixtures

These documents contain synthetic test data only. They were generated with the existing backend renderers at revision f8af47b1; no live queries or backend changes were needed.

- `inventory.html`: `backend/tests/report/test_inventory_aging_render.py::_fixture`, passed through the existing inventory aging compute/section builders and `render_report_html`.
- `financial.html`: `backend/tests/test_report_html.py::_is_model` and `_fs_spec`, passed through `render_report_html`.

Frontend tests compare every table, figure, SVG and native control before and after presentation. Browser tests exercise inventory disclosures, financial section toggles, narrow-screen scrolling, and the Command Center embed.
