"""Offline rendered-artifact preview harness for the CFO-grade statement redesign's
acceptance gate (Task 5). Reproduces ``playbooks.compose_playbook_report``'s exact
composition path -- recipe -> resolved payloads -> ``assemble_spec`` ->
``render_report_html`` -- with NO DB, NO network, NO LLM: every resolved payload is a
canned fixture from ``tests.fixtures.statement_fixture``, standing in for what a live
``netsuite_financial_report`` call (dispatched via ``refresh_service._execute_sources``)
would have returned.

This is the controller's eyeball gate for the redesign (see
``.claude/rules/report-design.md`` #2, "rendered-artifact acceptance gate" -- the slice
is not done when tests pass, it is done when the rendered artifact is actually viewed).

Usage:
    .venv/bin/python scripts/render_statement_preview.py --out-dir /tmp/previews

Writes four files into --out-dir and prints their paths, one per line:
  income_statement.html           full happy path: r1 (current) + r2 (prior) +
                                   r3 (YoY) + r4 (trailing-6-month trend) all resolve
  balance_sheet.html              full happy path: r1 + r2 (2-source recipe shape)
  trial_balance.html              full happy path: r1 + r2 (2-source recipe shape)
  income_statement_degraded.html  compare-degrade path: ONLY r1 resolves; r2/r3/r4 are
                                   entirely unresolved, exercising the "compare source
                                   outage" branch (Task 4 Risk 2 / statement_builder's
                                   degradation contract)

Fidelity notes -- every divergence from the real compose path is named here, never
introduced silently:

  * ``refresh_service._execute_sources`` is NOT called -- it dispatches a real tool
    call (DB + network). This harness substitutes its OUTPUT SHAPE directly: a
    ``{result_id: <extract_result_payload-shaped dict>}`` mapping drawn from
    ``tests.fixtures.statement_fixture``'s hand-checked payloads instead of a live
    SuiteQL round trip. Everything downstream of that mapping -- recipe authoring
    (``build_playbook_recipe``), assembly (``assemble_spec`` -> ``build_statement_model``),
    and rendering (``render_report_html`` / ``build_provenance``) -- is the UNMODIFIED
    production code, invoked exactly as ``compose_playbook_report`` invokes it.

  * The "degraded" variant reproduces ``_execute_sources``'s real degrade mechanic:
    on a non-required rid's failure, ``_execute_sources`` OMITS that rid from the
    payloads dict it returns (it is never a ``{"success": False}`` stand-in) -- so
    ``resolver(rid)`` raises ``KeyError`` for an unresolved compare rid, which
    ``report_service._resolve_financial_statement_section`` catches and treats as
    "that one comparison degrades, the statement still renders". This harness
    reproduces exactly that by using ``income_statement_payloads_missing_compare()``
    (r2/r3/r4 keys absent entirely from the payloads dict), NOT
    ``income_statement_payloads_failed_compare()`` -- the latter's
    ``{"success": False}`` payload shape is a fixture for testing
    ``build_statement_model`` directly against a raw tool-result (a lower boundary);
    it never actually reaches the resolver on the real compose path, where a failed
    source is omitted before ``assemble_spec`` ever sees it.

  * No ``Report`` row is created, no audit log written, no tenant context set -- this
    harness never touches the database, and imports no DB-backed module at call time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The venv's editable install (`_editable_impl_ecom_netsuite_suites.pth`) points
# `app`/`tests` at the MAIN checkout's `backend/`, not this git worktree's. A plain
# `python scripts/render_statement_preview.py` run from a worktree does NOT get cwd
# prepended to sys.path (that only happens for `-m`/pytest invocations) -- so without
# this, the harness would silently import and render the MAIN checkout's code, not
# this worktree's redesign, defeating the entire point of an eyeball gate for THIS
# branch. Force this worktree's own `backend/` (this script's parent directory) to the
# front of sys.path before any `app`/`tests` import.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.report.playbooks import build_playbook_recipe  # noqa: E402
from app.services.report.report_html import build_provenance, render_report_html  # noqa: E402
from app.services.report.report_service import assemble_spec  # noqa: E402
from tests.fixtures import statement_fixture as fx  # noqa: E402

_PERIOD = "Jun 2026"


def _render(playbook_key: str, payloads: dict[str, dict]) -> str:
    """The exact composition sequence ``playbooks.compose_playbook_report`` runs,
    minus ``_execute_sources`` (see module fidelity notes) -- same recipe builder, same
    resolver-lambda shape, same freshness/provenance construction, same renderer."""
    title, recipe = build_playbook_recipe(playbook_key, {"period": _PERIOD})
    spec = assemble_spec(title, recipe["sections"], lambda rid: payloads[rid])
    return render_report_html(
        spec,
        freshness={"composed_at": recipe["captured_at"], "refreshed_at": ""},
        # T2 gate M1: resolved_rids=set(payloads) matches compose_playbook_report's own
        # call exactly -- without it, the degraded preview's provenance block falsely
        # claimed every source "executed" even for a rid the fixture never resolved,
        # contradicting the in-statement "unavailable this run" watch chips above it.
        provenance=build_provenance(recipe["sources"], recipe["captured_at"], resolved_rids=set(payloads)),
    )


def _previews() -> dict[str, str]:
    """filename -> rendered HTML, in the fixed order documented in the module docstring."""
    return {
        "income_statement.html": _render("income_statement", fx.income_statement_payloads()),
        "balance_sheet.html": _render("balance_sheet", fx.balance_sheet_payloads()),
        "trial_balance.html": _render("trial_balance", fx.trial_balance_payloads()),
        "income_statement_degraded.html": _render("income_statement", fx.income_statement_payloads_missing_compare()),
    }


def main(argv: list[str] | None = None) -> list[Path]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Directory to write the four preview HTML files into (created if missing).",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for filename, html in _previews().items():
        path = out_dir / filename
        path.write_text(html, encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for _path in main():
        print(_path)
