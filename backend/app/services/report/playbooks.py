"""Playbook catalog — curated deterministic report recipes (no LLM in the loop).

Each playbook maps 1:1 to a netsuite_financial_report REPORT_TEMPLATE, so every
number is a statement-grade GL aggregate. build_playbook_recipe emits exactly
the recipe_json schema the refresh engine validates, which is what buys playbook
reports auto-refresh, versioning, and download with zero extra machinery.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

_PERIOD_RE = re.compile(r"^[A-Z][a-z]{2} \d{4}$")  # NetSuite period name: "Jun 2026"

PLAYBOOKS: dict[str, dict] = {
    "income_statement": {
        "name": "Income Statement",
        "description": "Statement-grade P&L for one accounting period, straight from the GL.",
        "params": [{"key": "period", "label": "Accounting period", "example": "Jun 2026"}],
        "table_label": "P&L by account",
    },
    "balance_sheet": {
        "name": "Balance Sheet",
        "description": "Balance Sheet as of the end of an accounting period (inception-to-date).",
        "params": [{"key": "period", "label": "As-of period", "example": "Jun 2026"}],
        "table_label": "Balances by account",
    },
    "trial_balance": {
        "name": "Trial Balance",
        "description": "All GL accounts with debit/credit totals for one accounting period.",
        "params": [{"key": "period", "label": "Accounting period", "example": "Jun 2026"}],
        "table_label": "Trial balance",
    },
}


def build_playbook_recipe(playbook_key: str, params: dict[str, str]) -> tuple[str, dict]:
    meta = PLAYBOOKS.get(playbook_key)
    if meta is None:
        raise ValueError(f"Unknown playbook: '{playbook_key}'")
    period = (params or {}).get("period", "").strip()
    if not _PERIOD_RE.match(period):
        raise ValueError("period must be a NetSuite period name like 'Jun 2026'")
    title = f"{meta['name']} — {period}"
    recipe = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        # No heading section: render_report_html already emits an outer <h1> from
        # assemble_spec's title — a recipe-authored heading here duplicated it
        # back-to-back in the rendered HTML.
        "sections": [
            {"type": "table", "result_id": "r1", "label": meta["table_label"]},
            {
                "type": "narrative",
                "markdown": (
                    f"{{{{result:r1.row_count}}}} GL lines. Generated deterministically by the "
                    f"{meta['name']} playbook — every figure is a GL aggregate; no model wrote a number."
                ),
            },
        ],
        "sources": {
            "r1": {
                "tool": "netsuite_financial_report",
                "params": {"report_type": playbook_key, "period": period},
                "connection_id": None,
            }
        },
    }
    return title, recipe


async def compose_playbook_report(db, *, playbook_key, params, tenant_id, actor_id):
    """Deterministic compose: recipe template → fail-closed source execution →
    frozen HTML → normal Report row. Reuses the refresh engine's execution seam
    on purpose — identical validation, identical failure semantics, and the
    resulting report auto-refreshes like any composed one."""
    from app.core.database import set_tenant_context
    from app.models.report import Report
    from app.services import audit_service
    from app.services.report.refresh_service import _execute_sources, _validated_sources
    from app.services.report.report_html import build_provenance, render_report_html
    from app.services.report.report_service import assemble_spec, referenced_result_ids

    title, recipe = build_playbook_recipe(playbook_key, params)
    correlation_id = f"report-playbook:{playbook_key}:{uuid.uuid4().hex[:8]}"

    await set_tenant_context(db, str(tenant_id))
    payloads = await _execute_sources(
        db,
        _validated_sources(recipe),
        referenced_result_ids(recipe["sections"]),
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type="user",
        correlation_id=correlation_id,
    )
    spec = assemble_spec(title, recipe["sections"], lambda rid: payloads[rid])
    html = render_report_html(
        spec,
        freshness={"composed_at": recipe["captured_at"], "refreshed_at": ""},
        provenance=build_provenance(recipe["sources"], recipe["captured_at"]),
    )

    # tool calls may commit (e.g. token refresh) — re-establish before RLS writes
    await set_tenant_context(db, str(tenant_id))
    report = Report(
        tenant_id=tenant_id,
        title=title,
        spec_json=spec,
        rendered_html=html,
        created_by=actor_id,
        recipe_json=recipe,
    )
    db.add(report)
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.compose",
        actor_id=actor_id,
        actor_type="user",
        resource_type="report",
        resource_id=str(report.id),
        correlation_id=correlation_id,
        payload={"playbook": playbook_key, "source_count": len(recipe["sources"])},
    )
    await db.commit()
    await set_tenant_context(db, str(tenant_id))
    await db.refresh(report)
    return report
