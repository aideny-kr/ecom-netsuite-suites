"""Read-only readiness for product skills; never an execution authorization.

The installed SKILL.md files are expertise, not scheduler plans. Bindings below
describe the minimum tool contract for their guided work. They do not import
development skills, infer executors from prose, or inspect company instructions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class SkillBlocker(BaseModel):
    code: str
    message: str
    action: str


class SkillSurface(BaseModel):
    surface: Literal["chat", "scheduled"]
    status: Literal["available", "blocked", "unsupported"]
    blockers: list[SkillBlocker] = Field(default_factory=list)


class SkillRequirement(BaseModel):
    key: str
    label: str
    satisfied: bool


class AgentSkillMetadata(BaseModel):
    name: str
    description: str
    triggers: list[str]
    slug: str
    kind: Literal["expertise", "playbook", "company_instructions"]
    version: str
    owner: str
    provenance: str
    inputs: list[str]
    outputs: list[str]
    requirements: list[SkillRequirement]
    execution_surfaces: list[SkillSurface]
    readiness_note: str


# Product-owned minimum contracts. An unreviewed new markdown skill fails closed
# until it has a binding; a generic MCP `query` never proves a specific source.
_BINDINGS = {
    "accounting_operations": ("transaction_evidence", "connection_permission"),
    "accounting_treatments": ("transaction_evidence", "connection_permission"),
    "accounting_verification": ("transaction_evidence", "netsuite_query", "connection_permission"),
    "netsuite_subledger": ("transaction_evidence", "netsuite_query", "connection_permission"),
    "ar_ap_aging_triage": ("netsuite_query", "financial_permission"),
    "books_review": ("ledger_query", "financial_permission"),
    "cash_flow_runway": ("financial_report", "financial_permission"),
    "csv_import_generator": ("record_metadata", "workspace_patch", "workspace_permission"),
    "gross_margin_bridge": ("netsuite_query", "financial_permission"),
    "inventory_check": ("netsuite_query",),
    "metabase_bi": ("metabase_query", "metabase_discovery"),
    "metabase_sql": ("metabase_query", "metabase_discovery"),
    "month_end_close": ("netsuite_query", "financial_report", "financial_permission"),
    "period_comparison": ("netsuite_query",),
    "pl_flux_variance": ("financial_report", "financial_permission"),
    "ratio_analysis": ("netsuite_query", "financial_permission"),
    "sales_by_platform": ("netsuite_query",),
}
_LABELS = {
    "connection_permission": "Connection access permission",
    "transaction_evidence": "Transaction investigation evidence tools",
    "netsuite_query": "NetSuite query access",
    "ledger_query": "A connected ledger query source",
    "financial_report": "NetSuite financial report tool",
    "financial_permission": "Financial reports permission",
    "record_metadata": "NetSuite record metadata tool",
    "workspace_patch": "Workspace patch tool",
    "workspace_permission": "Workspace editing permission",
    "metabase_query": "Metabase query tool",
    "metabase_discovery": "Metabase search and resource discovery",
}


def _capabilities(tools: list[dict], permissions: set[str]) -> dict[str, bool]:
    from app.services.chat.tools import parse_external_tool_name

    names = {t["name"] for t in tools}
    # Keep discovery and execution on the SAME connector. Never return IDs,
    # account labels, URLs, descriptions or schemas in the public catalog.
    metabase: dict[str, set[str]] = {}
    netsuite_query = "netsuite_suiteql" in names
    for tool in tools:
        parsed = parse_external_tool_name(tool["name"])
        if parsed is None:
            continue
        connector, raw = parsed
        tag = tool.get("description", "") or ""
        if tag.startswith("[metabase_mcp]"):
            metabase.setdefault(str(connector), set()).add(raw)
        if (tag.startswith("[netsuite_mcp]") or tag.startswith("[netsuite_mcp ·")) and raw == "ns_runCustomSuiteQL":
            netsuite_query = True
    mb_query = any("query" in n or {"construct_query", "execute_query"} <= n for n in metabase.values())
    mb_complete = any(
        {"search", "read_resource"} <= n and ("query" in n or {"construct_query", "execute_query"} <= n)
        for n in metabase.values()
    )
    return {
        "connection_permission": "connections.view" in permissions,
        "netsuite_query": netsuite_query,
        "ledger_query": netsuite_query or "bigquery_sql" in names or mb_complete,
        "financial_report": "netsuite_financial_report" in names,
        "financial_permission": "chat.financial_reports" in permissions,
        "record_metadata": "netsuite_get_metadata" in names,
        "workspace_patch": "workspace_propose_patch" in names,
        "workspace_permission": "workspace.manage" in permissions,
        "transaction_evidence": "transaction_ops_accounting_evidence" in names,
        "metabase_query": mb_query,
        "metabase_discovery": mb_complete,
    }


def resolve_catalog(skills: list[dict], tools: list[dict], permissions: set[str]) -> list[AgentSkillMetadata]:
    capabilities = _capabilities(tools, permissions)
    catalog = []
    for skill in skills:
        binding = _BINDINGS.get(skill["slug"])
        requirements = [
            SkillRequirement(key=key, label=_LABELS[key], satisfied=capabilities[key]) for key in binding or ()
        ]
        blockers = [
            SkillBlocker(
                code="permission_required" if r.key.endswith("permission") else "tool_unavailable",
                message=f"{r.label} is unavailable.",
                action=(
                    "Ask a company administrator to review your permissions."
                    if r.key.endswith("permission")
                    else "Ask an administrator to connect or reauthorize the required source and verify its tools."
                ),
            )
            for r in requirements
            if not r.satisfied
        ]
        if binding is None:
            blockers.append(
                SkillBlocker(
                    code="binding_missing",
                    message="This skill's tool requirements are not verified.",
                    action="A product maintainer must register its capability contract.",
                )
            )
        content = Path(skill["_path"]).read_bytes()
        version = hashlib.sha256(content + json.dumps(binding).encode()).hexdigest()
        catalog.append(
            AgentSkillMetadata(
                **{k: skill[k] for k in ("name", "description", "triggers", "slug")},
                kind="expertise",
                version=version,
                owner="Suite Studio",
                provenance=f"product/skills/{skill['slug']}/SKILL.md",
                inputs=[
                    "User request, selected source and scope",
                    "Current source evidence and applicable company policy",
                ],
                outputs=["Guided analysis or a reviewable proposal with source evidence"],
                requirements=requirements,
                execution_surfaces=[
                    SkillSurface(surface="chat", status="blocked" if blockers else "available", blockers=blockers),
                    SkillSurface(
                        surface="scheduled",
                        status="unsupported",
                        blockers=[
                            SkillBlocker(
                                code="no_execution_binding",
                                message="Expertise instructions are not a registered scheduled playbook.",
                                action=(
                                    "Use the workflow builder's supported execution steps; "
                                    "this slash command cannot be scheduled directly."
                                ),
                            )
                        ],
                    ),
                ],
                readiness_note=(
                    "Based on current tool inventory and permissions, not a live source check. "
                    "Required inputs, record access, evidence and action approvals are checked during execution."
                ),
            )
        )
    return catalog


async def get_catalog(db, user) -> list[AgentSkillMetadata]:
    from app.core.dependencies import has_permission
    from app.services.chat.orchestrator import _check_connection_health, _filter_tools_for_dead_connections
    from app.services.chat.skills import get_all_skills_metadata
    from app.services.chat.tools import build_all_tool_definitions
    from app.services.feature_flag_service import is_enabled

    permissions = {
        p
        for p in ("chat.financial_reports", "workspace.manage", "connections.view")
        if await has_permission(db, user.id, p)
    }
    tools = await build_all_tool_definitions(db, user.tenant_id)
    tools = _filter_tools_for_dead_connections(tools, await _check_connection_health(db, user.tenant_id))
    if not (await is_enabled(db, user.tenant_id, "celigo") and await is_enabled(db, user.tenant_id, "reconciliation")):
        tools = [t for t in tools if not t["name"].startswith("transaction_ops_")]
    return resolve_catalog(get_all_skills_metadata(), tools, permissions)
