"""Compact, additive receipts for guidance actually supplied to an agent.

These describe exposure to guidance and returned tool calls, not financial
authority or proof that the model followed every instruction.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path


def load_skill_snapshot(slug: str) -> dict | None:
    from app.services.chat.skills import _FRONTMATTER_RE, get_all_skills_metadata
    from app.services.skill_catalog import skill_version

    metadata = next((s for s in get_all_skills_metadata() if s["slug"] == slug), None)
    if metadata is None:
        return None
    try:
        content = Path(metadata["_path"]).read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    match = _FRONTMATTER_RE.match(text)
    instructions = (text[match.end() :] if match else text).strip()
    if not instructions:
        return None
    return {
        "slug": slug,
        "name": metadata["name"],
        "version": skill_version(slug, content),
        "revision": sha256(instructions.encode()).hexdigest(),
        "instructions": instructions,
    }


def record_skill(snapshot: dict, selection: str, receipts: list[dict] | None) -> dict:
    receipt = {key: snapshot[key] for key in ("slug", "version", "revision")}
    receipt["selection"] = selection
    if receipts is not None and receipt not in receipts:
        receipts.append(receipt)
    return receipt


def skill_instructions(slug: str, selection: str, receipts: list[dict] | None) -> str:
    snapshot = load_skill_snapshot(slug)
    if snapshot is None:
        return ""
    record_skill(snapshot, selection, receipts)
    return snapshot["instructions"]


_CONTEXT_TOOLS = {
    "netsuite_accounting_context",
    "transaction_ops_accounting_evidence",
    "transaction_ops.accounting_evidence",
    "transaction_ops_accounting_group",
    "transaction_ops.accounting_group",
}


def tool_provenance(tool_name: str, result: dict) -> dict:
    """Read only local trusted tool contracts; never infer provenance from prose."""
    extra = {}
    if tool_name == "agent_skill" and result.get("success") is True:
        if all(isinstance(result.get(k), str) for k in ("slug", "version", "revision")):
            extra["skill_receipt"] = record_skill(result, "tool", None)
    if tool_name not in _CONTEXT_TOOLS or result.get("success") is False or result.get("error"):
        return extra
    manifests = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "context_provenance" and isinstance(item, dict):
                    # Omit claims, owner names and source payloads. Preserve the
                    # exact returned scope/status even when stale or unapproved.
                    receipt = {
                        k: deepcopy(item[k])
                        for k in ("version", "config_id", "company_scope", "binding_sha256", "status", "scope_required")
                        if k in item
                    }
                    receipt["entries"] = [
                        {
                            k: deepcopy(entry[k])
                            for k in ("key", "revision", "content_sha256", "scope", "kind", "status", "scope_match")
                            if k in entry
                        }
                        for entry in item.get("entries", [])
                        if isinstance(entry, dict)
                    ]
                    if receipt not in manifests:
                        manifests.append(receipt)
                else:
                    visit(item)

    visit(result)
    if manifests:
        extra["context_receipts"] = manifests
    return extra


def execution_receipt(skills: list[dict], calls: list[dict]) -> dict:
    from app.services.chat.tool_call_results import tool_call_had_error
    from app.services.chat.tools import parse_external_tool_name

    loaded = deepcopy(skills)
    contexts, tools = [], []
    for call in calls:
        skill = call.get("skill_receipt")
        if isinstance(skill, dict) and skill not in loaded:
            loaded.append(deepcopy(skill))
        name = call.get("tool", "")
        parsed = parse_external_tool_name(name)
        tools.append(
            {
                "tool": name,
                "connector_id": str(parsed[0]) if parsed else None,
                "step": call.get("step"),
                "outcome": "error" if tool_call_had_error(call) else "returned",
            }
        )
        for receipt in call.get("context_receipts", []):
            if receipt not in contexts:
                contexts.append(deepcopy(receipt))
    return {"version": 1, "skills": loaded, "tools": tools, "contexts": contexts}


def persist_execution_receipt(output: dict | None, receipt: dict | None) -> dict | None:
    """Preserve the existing card/report envelope and every result identifier."""
    if receipt is None:
        return output
    return {**(output or {}), "execution_receipt": deepcopy(receipt)}
