---
name: suite-studio-netsuite
description: Placeholder for the Suite Studio NetSuite skill pack. Loaded by the vendored Hermes Agent runtime once the third /goal wires ns_runSuiteQL.
platforms: [linux, macos, windows]
---

# Suite Studio NetSuite

This skill is a **placeholder**. It locks the on-disk location the Suite Studio Desktop runtime will load the NetSuite skill pack from, validates the [agentskills.io](https://agentskills.io) format (name + description frontmatter + markdown body), and gives the third `/goal` a concrete file to fill in.

## When this skill becomes active

Not yet. The third `/goal` in the Desktop v0 roadmap wires `ns_runSuiteQL` into the sidecar's `AIAgent` and fills this body with real content — read-only SuiteQL query patterns, schema cheatsheets, and HITL guardrail rules.

Until then, the runtime may load this skill but it is intentionally a no-op.

## See also

- [`README.md`](README.md) in this directory — productization plan summary
- `desktop/SPIKE-RESULTS.md` — B0 pre-flight that confirmed the agentskills.io compatibility surface
- The in-tree Hermes Agent `skills/note-taking/obsidian/SKILL.md` — reference for the format
