# Skill execution receipts

Chat uses the existing unified agent, maintained application skills and final
authorized tool inventory. Selecting a skill supplies guidance; it does not
grant tools, change the selected source, establish company policy or approve a
financial action. Scheduled execution remains a separate supported contract.

Completed agent responses retain an additive `execution_receipt` in the saved
message's `structured_output`. Existing cards, charts, result IDs, streaming
events and tool results retain their formats. The receipt is available when
the authorized user reloads conversation history; no new public audit feed is
created.

- `skills`: slug, catalog `version`, instruction-body `revision`, and selection
  mode (`explicit`, `matched`, `connected`, `workflow`, or `tool`). The catalog
  version hashes the exact installed file and its maintained capability binding.
  The body hash preserves compatibility with existing skill-load audits.
- `tools`: calls recorded by the actual agent loop, their connector IDs when
  external, steps and outcomes. `returned` means a tool response was received;
  it does not mean a business outcome was independently verified. Errors remain
  distinguishable, as do requests awaiting confirmation. Entries without a
  captured outcome are `unclassified`. Available-but-unused tools are not listed.
- `contexts`: version, configuration binding and scoped entry revisions from
  the existing local accounting-context/evidence tool contracts. Draft, stale,
  mismatched and invalidated states are retained. Claims, instructions and source
  payloads are not copied into this compact receipt. No context receipt means
  no such context was returned by these tools, not that company policy is absent.

Skill and context receipts also remain beside the relevant tool-call summaries,
so summary truncation does not erase their versions. Connected Metabase guidance
is derived from the final filtered connector inventory. Explicit skills may be
loaded when their dependencies are unavailable; execution still fails at the
existing permission, connection, source-evidence and approval boundaries.

Receipts attest which maintained guidance was supplied, not whether a model
followed every instruction. They contain no implicit approval. Per-turn state is
reset before agent reuse; source-clarification replies do not claim that the
execution prompt ran. The legacy chat path retains its connected-skill/tool
receipts as well. Onboarding is unchanged.
