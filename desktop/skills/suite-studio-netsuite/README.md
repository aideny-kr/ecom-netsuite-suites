# Suite Studio NetSuite skill pack — placeholder

This directory is the home of the **Suite Studio NetSuite skill** that will be loaded by the vendored Hermes Agent runtime once the third `/goal` lands.

## Status: placeholder

The file [`SKILL.md`](SKILL.md) is a minimal valid [agentskills.io](https://agentskills.io) stub (name + description frontmatter + markdown body). It is intentionally non-functional in this `/goal` — its purpose is to:

1. Lock in the on-disk location the runtime will load from
2. Validate the agentskills.io format end-to-end before we author real skill content
3. Give the third `/goal` (which wires `ns_runSuiteQL` into the sidecar's `AIAgent`) a concrete target to fill in

## Productization plan summary

The skill pack will eventually bundle:

- **Query patterns** — pre-verified SuiteQL snippets covering common NetSuite questions ("list subsidiaries", "find an item by display name", "show me open POs from vendor X")
- **Schema cheatsheets** — record-type → field-name maps loaded from the same source of truth that backs `backend/app/services/chat/knowledge_profiles/netsuite.yaml`
- **Mutation guardrails** — Suite Studio's HITL gate semantics mirrored as skill-level guidance ("never `ns_createRecord` without a confirmation card")

Implementation cadence:

| `/goal` | What lands here |
|---|---|
| 3rd (next) | Wire `ns_runSuiteQL` MCP server + first read-only SKILL.md content. Smoke test: `python runtime/sidecar.py "list my subsidiaries"`. |
| 4th | + Obsidian-memory-MCP, entity registry vault scaffolding |
| 6th | + HITL audit_events flow, write-back guardrails |

Until the 3rd `/goal` lands, this directory contains only the stub.
