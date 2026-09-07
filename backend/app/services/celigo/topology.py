"""Pure projections over synced Celigo objects -- no DB, no I/O.

The sync keeps a flow's routers/branches verbatim in `celigo_flows.raw_json`
(sanitized); the step table only carries router_id/branch_id per row. The
canvas needs the DECLARED side: branch names, order, rules, the router chain
(`nextRouterId`) and the routing mode. This module is the one place that reads
those keys, so a Celigo rename is a one-file change."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.celigo import CeligoScript


def adaptor_family(adaptor_type: str | None) -> str | None:
    """Coarse connector family for the dashboard's per-integration
    `adaptor_families` list -- Celigo's own `adaptor_type` values are
    connector-and-direction-specific (`NetSuiteExport`,
    `NetSuiteDistributedImport`, `HTTPExport`, ...); this collapses them to
    the vendor/protocol a person recognizes. Checked in a fixed order --
    NetSuite, then AS2, FTP, RDBMS, REST, HTTP -- so the result is
    deterministic if a name were ever to contain more than one keyword.
    `None` for an unmapped or absent adaptor_type -- never invented."""
    if not adaptor_type:
        return None
    lowered = adaptor_type.lower()
    if "netsuite" in lowered:
        return "NetSuite"
    for keyword, family in (("as2", "AS2"), ("ftp", "FTP"), ("rdbms", "RDBMS"), ("rest", "REST"), ("http", "HTTP")):
        if keyword in lowered:
            return family
    return None


def step_kind(role: str, adaptor_type: str | None) -> str:
    """Celigo's own vocabulary: Source (generator), Lookup (a processor that is an
    export), Destination (any other processor)."""
    if role == "generator":
        return "source"
    if adaptor_type and adaptor_type.lower().endswith("export"):
        return "lookup"
    return "destination"


def count_rules(rules: object) -> int:
    """A Celigo filter is one expression `[op, lhs, rhs]`; `["and"|"or", expr, ...]`
    combines several. Count expressions, not list elements."""
    if not isinstance(rules, list) or not rules:
        return 0
    head = rules[0]
    if isinstance(head, str) and head.lower() in ("and", "or"):
        return sum(1 for r in rules[1:] if isinstance(r, list))
    return 1


def project_routers(raw_json: object) -> list[dict]:
    routers = raw_json.get("routers") if isinstance(raw_json, dict) else None
    out: list[dict] = []
    for r in routers if isinstance(routers, list) else []:
        if not isinstance(r, dict):
            continue
        branches: list[dict] = []
        raw_branches = r.get("branches")
        for order, b in enumerate(raw_branches if isinstance(raw_branches, list) else []):
            if not isinstance(b, dict):
                continue
            input_filter = b.get("inputFilter")
            rules = input_filter.get("rules") if isinstance(input_filter, dict) else None
            processors = b.get("pageProcessors")
            branches.append(
                {
                    "id": b.get("branchId") or b.get("id"),
                    "name": (b.get("name") or None) if isinstance(b.get("name"), str) else None,
                    "rule_count": count_rules(rules),
                    "next_router_id": b.get("nextRouterId") or None,
                    "order": order,
                    "declared_step_count": len(processors) if isinstance(processors, list) else 0,
                }
            )
        script = r.get("script")
        out.append(
            {
                "id": r.get("id"),
                "name": (r.get("name") or None) if isinstance(r.get("name"), str) else None,
                "route_records_to": r.get("routeRecordsTo") or None,
                "route_records_using": r.get("routeRecordsUsing") or None,
                "has_script_slot": isinstance(script, dict) and bool(script),
                "branches": branches,
            }
        )
    return out


@dataclass(frozen=True)
class ScriptFamilyFact:
    name: str
    size_chars: int | None
    copies_count: int
    versions_count: int
    version_letter: str | None
    content_diverged: bool


def assign_version_letters(members: list[CeligoScript]) -> dict[str, str]:
    """Map each distinct `content_hash` among *members* (one already-grouped
    clone family) to a version letter (A, B, C...), ordered by first
    appearance: the earliest `celigo_last_modified` across that hash's own
    members, ties broken by the hash string itself for a deterministic
    order independent of row-insertion order. A member with `content_hash is
    None` never contributes a hash and therefore never gets a letter or
    creates a version -- absence of content is not version zero.

    Shared by `script_family_facts` below (whose own single-copy-family
    special case is layered ON TOP of this, not baked into it) and by
    `app.services.celigo.script_families` (which uses the letters exactly as
    returned here, including for a single-copy family -- the Scripts view
    always shows a version card for a family with content, unlike the flow
    map's inline chip)."""
    timestamps_by_hash: dict[str, list] = {}
    for s in members:
        if s.content_hash is None:
            continue
        timestamps_by_hash.setdefault(s.content_hash, [])
        if s.celigo_last_modified is not None:
            timestamps_by_hash[s.content_hash].append(s.celigo_last_modified)
    first_seen: dict[str, object] = {
        content_hash: (min(seen) if seen else None) for content_hash, seen in timestamps_by_hash.items()
    }
    ordered_hashes = sorted(first_seen, key=lambda h: (first_seen[h] is None, first_seen[h], h))
    return {content_hash: chr(ord("A") + i) for i, content_hash in enumerate(ordered_hashes)}


def script_family_facts(scripts: list[CeligoScript]) -> dict[uuid.UUID, ScriptFamilyFact]:
    """Per script row: how many copies its clone family (`dedup_key`) has, how many
    differing versions (distinct content_hash), and which version letter THIS row
    runs -- letters assigned by the family's earliest `celigo_last_modified`
    (None sorts last), so A is the oldest text. A single-copy family gets no letter."""
    by_family: dict[str, list[CeligoScript]] = {}
    for s in scripts:
        by_family.setdefault(s.dedup_key, []).append(s)
    facts: dict[uuid.UUID, ScriptFamilyFact] = {}
    for members in by_family.values():
        letters = assign_version_letters(members)
        versions = max(len(letters), 1)
        for s in members:
            letter = letters.get(s.content_hash) if len(members) > 1 and s.content_hash is not None else None
            facts[s.id] = ScriptFamilyFact(
                name=s.name,
                size_chars=len(s.content) if s.content is not None else None,
                copies_count=len(members),
                versions_count=versions,
                version_letter=letter,
                content_diverged=len(letters) > 1,
            )
    return facts
