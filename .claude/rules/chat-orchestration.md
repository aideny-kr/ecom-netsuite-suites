---
description: Chat / agent / tool orchestration gotchas. Loads when editing the chat pipeline.
paths:
  - backend/app/services/chat/**
  - backend/app/mcp/**
  - backend/app/api/v1/chat*.py
---

# Chat orchestration rules

## Agent + tool wiring
1. **Agent hallucination guard** — `_task_contains_query()` in `base_agent.py` forces tool execution at step==0.
2. **MCP tool visibility is role-permission based** — missing tools = OAuth role lacks permissions, NOT SuiteApp version. Record Tools need `REST Web Services (Full)` + Create/Edit. Saved Search needs `Perform Search (Full)`. Administrator role CANNOT be used.
3. **External MCP response format differs** — `ns_runCustomSuiteQL` returns `{"data": [{col: val}], ...}`, NOT `{"columns": [], "rows": []}`. Test interception with both formats.
4. **Knowledge profiles use fnmatch globs** — `ext__*__ns_createRecord` matches any connector UUID. Never hardcode connector UUIDs in `trigger_tools`.
5. **Profile prompt fragments must stay in sync** — when updating SuiteQL rules in the unified agent prompt, also update the relevant knowledge profile YAML.
6. **RAG partitions are per-profile** — `collect_rag_partitions()` gathers IDs from active profiles. Pass `partition_ids` to `retrieve_domain_knowledge()`.
7. **BigQuery tool names** — `bigquery.sql` in registry becomes `bigquery_sql` for the LLM. Sanitizer handles this automatically.
8. **BigQuery uses `LIMIT`** — `FETCH FIRST N ROWS ONLY` is SuiteQL only.
9. **Chart extraction happens post-stream** — `extract_charts()` runs after full response, emits `chart` SSE events.

## Tool result + SSE pipeline
10. **Tool result interception** — `_intercept_tool_result()` emits SSE `data_table`/`financial_report`, condenses for LLM.
11. **`session.source_pin` is a prompt hint** — injected via `build_source_pin_hint()`. Model decides whether to follow. No routing override.
12. **Auto source_pin from tool use** — `_compute_source_pin_update(tool_calls_log)` updates `session.source_pin` post-turn. Mixed turns clear the pin.
13. **`SessionDetailResponse` must include run fields** — `active_run_id`, `status`, `run_started_at` in BOTH `SessionListItem` and `SessionDetailResponse`.

## Mutation safety
14. **Mutation tools require HITL** — `classify_mutation()` detects writes; `base_agent.run_streaming()` yields `confirmation_required` BEFORE execution. Non-streaming `run()` blocks mutations entirely. Never call `execute_tool_call()` directly without `validate_and_extract_confirmation()`.
15. **HMAC session binding** — `generate_confirmation_token()` binds payload to session_id (falls back to tenant_id, not empty string). One-use tokens.

## SQL read-only validator
16. **`_validate_read_only` must strip SQL comments** — LLMs generate `-- comment\nSELECT...`. `_strip_sql_comments()` removes `--` and `/* */` before `startswith`. Do NOT use it to transform queries before execution (doesn't handle string literals).

## Adapter timeouts
17. **LLM adapter SDK defaults hang for 10 min** — Always pass `timeout=httpx.Timeout(connect=5, read=60, write=60, pool=60)` and `max_retries=2`. Same for OpenAI/Gemini. Wrap pre-flight resolver calls in `asyncio.wait_for(timeout=15)`.
18. **Anthropic adapter must allowlist tool fields** — `_to_api_tool()` allowlists `name`, `description`, `input_schema`, `cache_control`, `type`. Internal-only fields like `category` must NOT pass through. Regression test: `TestAnthropicToolFieldStripping`.

## Orchestrator control flow
19. **Initialize orchestrator variables before branch points** — variables used after if/elif chains in `run_chat_turn()` MUST be initialized before the chain. `test_orchestrator_paths.py` catches this statically.
20. **Never hardcode tool names in agent prompts** — use the `{{TOOL_INVENTORY}}` placeholder, resolved by `_assemble_system_prompt`. CI invariant `tests/test_prompt_tool_sync.py` fails on hardcoded names. Add tool categories in ONE place: `tool_categories.py::_EXACT`.
