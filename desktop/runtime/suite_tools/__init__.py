"""Desktop-local Hermes tools for Suite Studio.

Named `suite_tools` (NOT `tools`) deliberately: Hermes owns the top-level
`tools` package via its editable meta-path finder, so a `desktop/runtime/tools/`
package would shadow it and break `tools.registry`. See
`desktop/runtime/orchestration/README.md` for the evidence.
"""
