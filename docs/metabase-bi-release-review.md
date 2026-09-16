# Independent release review — PASS after fixes

Reviewed `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/release-metabase-bi-staging`, exactly `d0d2aed7..423448edd87357bc7c3f2357a45200daa3ce8753`. Read-only review; no source edits, merges, deployment, or duplicate regression suite.

## Original findings — both resolved by 97e8436a

1. **P1 — A successful native MBQL query breaks the next chat turn.** Candidate `backend/app/services/chat/tools.py:242` and `backend/app/services/chat/skills/metabase_sql/SKILL.md:50` explicitly direct the agent to execute a structured object in `query`. That object is preserved in the tool log (`agents/base_agent.py:2508`). Existing `history_tool_trace.py:98` returns it as SQL and `history_tool_trace.py:85` passes the dict to `re.sub`. The next UI message unconditionally loads this trace in `orchestrator.py:2923`, before the agent can respond. Reproduced on the exact candidate with `PYTHONPATH=backend python3`: `build_history_dicts` of an assistant tool call `params={"query":{"lib/type":"mbql/query","stages":[...]}}`, `result_summary="Returned 1 rows"`, raises `TypeError: expected string or bytes-like object`. Handle non-string query payloads as bounded structured parameters and retain string SQL behavior. Acceptance: successful MBQL query, persisted assistant log, then a follow-up turn succeeds with useful history.

2. **P2 — The new gate forgets a verified clarification-card source choice.** `backend/app/services/chat/source_selection.py:36-46` examines only user-message text, and `agents/unified_agent.py:982-988` supplies no persisted verified choice on later turns. Actual UI card selection sends `content: "Picked option B"` (`frontend/src/app/(dashboard)/chat/page.tsx:556`); its source is in the signed card payload/assistant structured output, not a user source-name string. The chosen turn bypasses the gate through `plan_mode_resume_source`, but the next follow-up returns the source question again. Reproduced with actual UI-shaped history (user asks count; assistant offers NetSuite/Metabase; user `Picked option B`; assistant reports Metabase result), then `Break those down by status`: helper returns `Which data source should I use for this question: Metabase or NetSuite?`. Persist/replay the verified card source as explicit user choice, keeping automatic tool-derived pins and arbitrary assistant prose excluded. Also ensure explicit textual source choices survive the history window: `orchestrator.py:2926` caps history before this gate. Acceptance: card choice and explicit text choice both persist into follow-ups, including after windowing; a newer user refusal/change supersedes an older choice.


## Focused fix verification — 97e8436ae7ea820d0a73152ccfaa7502a14e38a7

Reviewed only `423448ed..97e8436a` after the original bounded release review. **Both reported blockers are resolved; no remaining code-review blocker.**

- **P1 resolved:** `_extract_sql` accepts only nonempty string query/sqlQuery values, and `_compact_params` retains bounded structured-query context. Independently reran the original persisted MBQL history reproduction: history builds successfully, includes MBQL context, and does not label it SQL. A structured `query` plus valid `sqlQuery` retains string SQL behavior.
- **P2 resolved:** the orchestrator passes full tenant/session message dictionaries to source resolution before model-history windowing; the resolver accepts a persisted `status=chosen` card only with a unique matching A/B/C `chosen_id`, and returns canonical source identities. Both UnifiedAgent entry points pass the history and add the selected-source directive to the actual model prompt. The card summary also renders the chosen source. Independently reproduced actual UI `Picked option B` history, pending/rejected/superseded/manual cards, missing/mismatched/duplicate chosen IDs, newer refusal and source switch, and a textual choice older than 100 messages. All passed.
- Added regression cases cover the original defects and nonstreaming gate-to-prompt integration. Parent reports 270 focused tests passed with two existing fixture warnings; I inspected the new cases and did not duplicate that suite.

Remaining release validation: parent must complete the planned actual UI/API multi-turn smoke, including persisted MBQL follow-up. This is a deployment acceptance check, not an unresolved source finding. No merge or deployment performed by reviewer.

## Other reviewed boundaries

- Metabase read exemption requires the real tenant-scoped connector, custom provider, OAuth2, Metabase OAuth metadata, HTTPS native Cloud suffix, exact native path, allowed port, no userinfo/query/fragment, and six positively named read tools. Arbitrary custom hosts, SQL/write/unknown tools retain exact-call confirmation at both classification and dispatch. No new cross-tenant lookup or bypass identified.
- `get_mcp_connector` retains the tenant predicate; inventory is built from active enabled tenant connectors. Native OAuth/token-refresh implementation is preserved from the staging base.
- HTTP dispatch/HITL routing, NetSuite read catalog and financial-write interception, and Celigo guards remain present. No removed protection found in the reviewed integration diff.
- Skills attach through final filtered inventory, preserve exact connector tool groups, and explicitly defer to selected sources and permission boundaries. Metabase-specific streaming-loop changes suppress the generic early-stop behavior only for batches containing tagged Metabase calls; normal step/retry bounds remain.
- Metabase is added to Plan Mode source resolver, signed-card schema, frontend labels, and UUID-based source filtering.

## Verification scope

The initial two deterministic reproductions failed on 423448ed as described; both pass after 97e8436a. Parent is running the existing regression suite and deployment/UI smoke, so those were not duplicated. No security blocker beyond the functional release blockers was identified. A real browser/API multi-turn smoke is still required; the isolated single-turn model/MCP evidence does not exercise persisted-history replay or card-choice persistence.

Post-review deployment acceptance is recorded in [the staging release report](metabase-bi-staging-release.md).
