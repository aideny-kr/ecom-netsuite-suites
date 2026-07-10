# Recon Resolution Phase 2 — ResolutionAgent Tail + Chat Tools — Implementation Plan

> **For agentic workers:** executed via a build Workflow (sequential implement stages + per-task review + advisory multi-angle phase). Steps use checkbox (`- [ ]`) syntax. Every task is self-contained: exact files, code, tests, commands.

**Goal:** The LLM ResolutionAgent investigates planner abstentions (`needs_human` proposals from `source='planner'`) in the background and upgrades them to concrete proposals (`source='agent'`) — or enriches them with gathered evidence — under hard budgets, a no-LLM-numbers contract, and a new default-OFF flag. Chat gains `recon.get_resolution_summary` (read) and `recon.approve_group` (HITL confirmation card → same approval core as the page).

**Architecture:** Deterministic context gathering (DB-only: result + payout line + candidate NetSuite postings) → ONE forced-tool LLM classification call per item via the existing adapter stack (`get_adapter` + `get_tenant_ai_config`) → code-side output validation (action allowlist, materiality guard, numeric-token contract) → supersede-and-insert on the proposals table (same invariants as `plan_run`). Runs as an `InstrumentedTask` Celery task on the `recon` queue (job row = free progress tracking). Chat approve converges on a shared service core also used by the REST endpoint.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async, Celery (`InstrumentedTask`), the chat LLM adapter stack, pytest (local docker Postgres harness), Next.js/React Query/vitest.

**Spec:** `docs/superpowers/specs/2026-07-06-recon-summary-first-resolution-design.md` (Phase 2) + the addendum written in Task 9. Preconditions ticket 86bavax8u is closed by Tasks 1 and 9.

## Global Constraints

- TDD: failing test first, every task. DB tests: `cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite" DATABASE_URL_DIRECT="" .venv/bin/python -m pytest tests/<file> -v` (usually needs sandbox disabled). NEVER point tests at Supabase.
- All money is `Decimal`. The agent NEVER writes to NetSuite (proposals only). Amounts in agent narratives come ONLY from evidence values (Task 3 validator enforces; violation → degrade to `needs_human`).
- No schema migration in this phase (proposals table + jobs table suffice).
- New flag `recon_resolution_agent` defaults OFF. Agent dispatch requires it AND `reconciliation`. Chat mutation tool enforces `recon_resolution_ui` via the shared core (body-level check, matching `_ensure_resolution_ui_enabled`).
- The agent may output ONLY: `book_fee_line`, `create_and_apply_deposit`, `apply_deposit`, `writeoff_je` (sub-materiality only), `carry_forward`, `needs_human`. NEVER `credit_memo_refund` or `void_duplicate` (human-only policy).
- Agent budgets: 1 LLM call per item, `max_tokens=1024`, 45s per-item timeout, 50 items per run per task invocation.
- It only touches proposals where `source='planner' AND action='needs_human' AND status='proposed'`; supersede-then-insert per item, one transaction per item, skip if no longer eligible (human decided meanwhile).
- Lint: `cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .` on touched files; FE `npx vitest run && npx tsc --noEmit`.
- Commit per task on branch `feat/recon-resolution-phase2-agent`; never push (controller pushes). Commit messages end with:
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Chat/agent/profile changes must not regress the vs-MCP benchmark surface: no hardcoded tool names in prompts ({{TOOL_INVENTORY}} discipline), `test_prompt_tool_sync.py` updated in the same task that registers tools.

---

### Task 1: Chargeback policy gate precedes evidence rules (planner precedence fix)

**Files:**
- Modify: `backend/app/services/reconciliation/resolution_planner.py` (`plan_result` — move the chargeback check above the `deposit_unapplied` evidence rule; renumber docstring rules: 3=chargeback gate, 4=deposit_unapplied)
- Test: `backend/tests/test_resolution_planner.py` (add one test; adjust rule-number comments only if they name numbers)

**Interfaces:** `plan_result` signature unchanged; ordering contract: policy gates beat evidence rules.

- [ ] **Step 1: Write the failing test** (append to the existing file)

```python
def test_chargeback_gate_preempts_unapplied_evidence():
    """Policy gates beat evidence rules: a chargeback with deposit_unapplied
    evidence must still go to needs_human, never apply_deposit."""
    p = _plan(
        variance_type="chargeback", variance_amount=Decimal("42.00"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1", "deposit_unapplied": True},
    )
    assert p.action == "needs_human"
    assert p.root_cause == "chargeback"
```

- [ ] **Step 2: Run it — expect FAIL (current code hits the evidence rule first → apply_deposit)**
- [ ] **Step 3: Implement:** in `plan_result`, move the `if variance_type == "chargeback":` block ABOVE the `deposit_unapplied` block; update the module docstring rule list (chargeback becomes rule 3, evidence rule becomes 4) and the inline comments. No other logic changes.
- [ ] **Step 4: Run `tests/test_resolution_planner.py` (all) — expect PASS incl. the pre-existing rule-3 fee/unapplied test (fees+unapplied still → apply_deposit).**
- [ ] **Step 5: Commit** `fix(recon): chargeback policy gate precedes evidence rules in ResolutionPlanner`

---

### Task 2: `recon_resolution_agent` flag + agent-eligible query helper

**Files:**
- Modify: `backend/app/services/feature_flag_service.py` (DEFAULT_FLAGS)
- Create: `backend/app/services/reconciliation/resolution_agent.py` (start with constants + eligibility query only; Task 4 adds the core)
- Test: `backend/tests/test_resolution_agent_eligibility.py`

**Interfaces:**
- Produces: flag key `"recon_resolution_agent": False`; `AGENT_ALLOWED_ACTIONS` frozenset; `MAX_ITEMS_PER_RUN = 50`; `async def fetch_agent_eligible(db, tenant_id, run_id, limit=MAX_ITEMS_PER_RUN) -> list[ReconResolutionProposal]` returning planner-sourced `needs_human` `proposed` rows for the run, oldest first, capped.

- [ ] **Step 1: Failing test**

```python
from decimal import Decimal

from app.services.feature_flag_service import DEFAULT_FLAGS
from app.services.reconciliation.resolution_agent import (
    AGENT_ALLOWED_ACTIONS,
    MAX_ITEMS_PER_RUN,
    fetch_agent_eligible,
)
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


def test_flag_registered_default_off():
    assert DEFAULT_FLAGS.get("recon_resolution_agent") is False


def test_agent_action_policy():
    assert AGENT_ALLOWED_ACTIONS == frozenset(
        {"book_fee_line", "create_and_apply_deposit", "apply_deposit",
         "writeoff_je", "carry_forward", "needs_human"}
    )
    assert "credit_memo_refund" not in AGENT_ALLOWED_ACTIONS
    assert "void_duplicate" not in AGENT_ALLOWED_ACTIONS
    assert MAX_ITEMS_PER_RUN == 50


async def test_fetch_agent_eligible_filters(db, tenant_a):
    from app.api.v1.reconciliation import plan_resolutions
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    # one manual_adjustment (eligible after planning) + one fee (not needs_human)
    await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="needs_review",
        match_type="deterministic", variance_type="manual_adjustment",
        variance_amount=Decimal("77.10"), stripe_amount=Decimal("500.00"),
        netsuite_amount=Decimal("422.90"),
        evidence={"charge_source_id": "ch_m", "order_reference": "R9"},
    )
    await create_test_recon_result(
        db, tenant_a.id, run.id, status="pending", bucket="auto_classifications",
        match_type="deterministic", variance_type="fees",
        variance_amount=Decimal("9.00"), stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_f", "order_reference": "R1"},
    )
    await db.flush()
    # enable the UI flag for the mutation endpoint (test-tenant has no flag rows)
    from app.models.feature_flag import TenantFeatureFlag
    db.add(TenantFeatureFlag(tenant_id=tenant_a.id, flag_key="recon_resolution_ui", enabled=True))
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)

    eligible = await fetch_agent_eligible(db, tenant_a.id, run.id)
    assert len(eligible) == 1
    assert eligible[0].action == "needs_human"
    assert eligible[0].source == "planner"
```

- [ ] **Step 2: Run — expect FAIL (module missing)**
- [ ] **Step 3: Implement**

`feature_flag_service.py` DEFAULT_FLAGS gains:

```python
    # Phase 2 of the summary-first recon rework: background LLM agent that
    # investigates planner abstentions. LLM-cost surface — default OFF.
    "recon_resolution_agent": False,
```

`resolution_agent.py` (module head):

```python
"""ResolutionAgent — Phase 2 of the summary-first recon rework.

Investigates planner abstentions (source='planner', action='needs_human',
status='proposed') with ONE forced-tool LLM classification call per item over
deterministically gathered DB context. Output is validated code-side (action
allowlist, materiality guard, numeric-token contract) and applied as a
supersede-and-insert (source='agent') under the same invariants as plan_run.
The agent NEVER writes to NetSuite and NEVER touches human/decided proposals.
"""

from __future__ import annotations

import uuid as _uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconResolutionProposal

AGENT_ALLOWED_ACTIONS = frozenset(
    {"book_fee_line", "create_and_apply_deposit", "apply_deposit",
     "writeoff_je", "carry_forward", "needs_human"}
)
MAX_ITEMS_PER_RUN = 50
PER_ITEM_TIMEOUT_SECONDS = 45
AGENT_MAX_TOKENS = 1024


async def fetch_agent_eligible(
    db: AsyncSession,
    tenant_id,
    run_id,
    limit: int = MAX_ITEMS_PER_RUN,
) -> list[ReconResolutionProposal]:
    """Planner abstentions the agent may investigate, oldest first, capped."""
    P = ReconResolutionProposal
    return list(
        (
            await db.execute(
                select(P)
                .where(
                    P.tenant_id == tenant_id,
                    P.run_id == run_id,
                    P.source == "planner",
                    P.action == "needs_human",
                    P.status == "proposed",
                )
                .order_by(P.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()
    )
```

- [ ] **Step 4: Run the test file — PASS. Also run `tests/test_resolution_flag_and_evidence.py` (flag-registry regression).**
- [ ] **Step 5: Commit** `feat(recon): recon_resolution_agent flag (default off) + agent eligibility query`

---

### Task 3: No-LLM-numbers narrative contract validator (pure)

**Files:**
- Create: `backend/app/services/reconciliation/narrative_contract.py`
- Test: `backend/tests/test_narrative_contract.py`

**Interfaces:** `numeric_tokens(text: str) -> set[str]` (normalized decimal strings found in text) and `narrative_respects_evidence(narrative: str, evidence_values: list[str]) -> bool` — True iff every numeric token in the narrative appears among the normalized numeric tokens of the provided evidence values.

- [ ] **Step 1: Failing test**

```python
from app.services.reconciliation.narrative_contract import (
    narrative_respects_evidence,
    numeric_tokens,
)


def test_numeric_tokens_normalizes():
    assert numeric_tokens("Fee of $1,284.55 across 3 payouts") == {"1284.55", "3"}
    assert numeric_tokens("no numbers here") == set()
    assert numeric_tokens("Charge ch_123abc for R628489275") == set()  # ids are not numbers


def test_respects_when_all_numbers_from_evidence():
    ev = ["variance_amount=77.10", "stripe_amount=500.00", "order R628489275"]
    assert narrative_respects_evidence(
        "Variance of $77.10 against a $500.00 charge — unexplained residual.", ev
    ) is True


def test_violates_on_invented_number():
    ev = ["variance_amount=77.10"]
    assert narrative_respects_evidence("Roughly $80 of unexplained variance.", ev) is False


def test_empty_narrative_ok():
    assert narrative_respects_evidence("", ["x=1"]) is True
```

- [ ] **Step 2: Run — FAIL** 
- [ ] **Step 3: Implement**

```python
"""No-LLM-numbers contract for agent narratives.

An agent narrative may contain a numeric token ONLY if that exact normalized
number appears in the evidence provided to the model. Violation → the caller
degrades the item to needs_human (never ships an invented figure).
Identifiers are exempt: tokens embedded in id-like words (ch_123, R628489275)
are not treated as numbers.
"""

from __future__ import annotations

import re

# Numbers NOT preceded/followed by id-ish characters (letters, _, digits-run
# glued to letters). Allows $ and thousands separators; captures decimals.
_NUM_RE = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w.])")


def _normalize(whole: str, frac: str | None) -> str:
    return whole.replace(",", "") + (frac or "")


def numeric_tokens(text: str) -> set[str]:
    return {_normalize(m.group(1), m.group(2)) for m in _NUM_RE.finditer(text or "")}


def narrative_respects_evidence(narrative: str, evidence_values: list[str]) -> bool:
    allowed: set[str] = set()
    for v in evidence_values:
        allowed |= numeric_tokens(str(v))
    return numeric_tokens(narrative) <= allowed
```

- [ ] **Step 4: Run — PASS (4 tests).**
- [ ] **Step 5: Commit** `feat(recon): no-LLM-numbers narrative contract validator`

---

### Task 4: ResolutionAgent core — gather, classify (one LLM call), validate, apply

**Files:**
- Modify: `backend/app/services/reconciliation/resolution_agent.py` (append)
- Test: `backend/tests/test_resolution_agent_core.py`

**Interfaces:**
- `gather_context(db, tenant_id, proposal) -> dict` — result row fields + evidence + up to 5 candidate `NetsuitePosting` rows (same tenant, amount within ±5% of `stripe_amount` or matching `order_reference` in memo/related fields — read-only, tenant-scoped) + payout line detail if `charge_payout_line_id` present. Every value stringified.
- `CLASSIFY_TOOL: dict` — one tool schema `classify_resolution` with input `{action: enum(AGENT_ALLOWED_ACTIONS), narrative: string, key_evidence: string[]}`.
- `classify_item(adapter, model, context) -> dict` — calls `adapter.create_message(model=model, max_tokens=AGENT_MAX_TOKENS, system=_AGENT_SYSTEM, messages=[...context...], tools=[CLASSIFY_TOOL], tool_choice={"type": "tool", "name": "classify_resolution"})`, returns the tool-use block input.
- `validate_output(out: dict, context: dict, materiality: tuple[Decimal, Decimal]) -> dict` — enforces allowlist, `writeoff_je` sub-materiality only, narrative contract (evidence values = flattened context values); any violation returns a `needs_human` dict with a `"contract_violation"` note.
- `async def apply_agent_proposal(db, proposal, out) -> bool` — one transaction: re-check eligibility (`status=='proposed'` etc.), supersede the planner row, insert the agent row (`source='agent'`, action/vehicle/group_key via `VEHICLE_BY_ACTION`/`group_key_for`, narrative from `out`, evidence = old evidence + `{"agent_key_evidence": [...]}`, `proposed_amount`/currency/above_materiality/charge_source_id copied from the planner row), commit; returns False (no-op) if no longer eligible.
- `_AGENT_SYSTEM` prompt: instructs classification ONLY from provided context, verbatim numbers only, no NetSuite writes, choose `needs_human` when unsure. No tool names in it (capability-sync safe).

- [ ] **Step 1: Failing tests** — use a `FakeAdapter` implementing `create_message` returning a canned `LLMResponse`-shaped object with one tool_use block (mirror `LLMResponse` fields from `backend/app/services/chat/llm_adapter.py` — read it first). Cases:

```python
# 1. happy path: fake returns action=book_fee_line + narrative using only context numbers
#    → validate_output passes → apply_agent_proposal supersedes planner row, inserts
#    source='agent' row with group_key 'manual_adjustment:book_fee_line:deposit'…
#    wait — root_cause stays the PLANNER row's root_cause; assert group_key recomputed
#    from (planner.root_cause, out.action, VEHICLE_BY_ACTION[out.action]).
# 2. invented number in narrative → validate_output returns needs_human w/ contract_violation.
# 3. disallowed action (credit_memo_refund) → needs_human.
# 4. writeoff_je above materiality (proposal.above_materiality=True) → needs_human.
# 5. apply_agent_proposal no-ops (returns False, no insert) when the planner row was
#    approved/superseded meanwhile.
# 6. gather_context returns stringified values incl. candidate postings, tenant-scoped
#    (seed a posting for tenant_b — must not appear).
```

Write these as real tests with the conftest factories (seed result → plan → grab the needs_human proposal). Full assertions on DB state after apply (statuses, source, group_key, one-active-per-result invariant preserved).

- [ ] **Step 2: Run — FAIL (functions missing)**
- [ ] **Step 3: Implement** per the interfaces above. Key code shapes:

```python
CLASSIFY_TOOL = {
    "name": "classify_resolution",
    "description": "Classify one reconciliation exception into a resolution action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(AGENT_ALLOWED_ACTIONS)},
            "narrative": {
                "type": "string",
                "description": "One-paragraph explanation. Use ONLY numbers that appear verbatim in the provided context.",
            },
            "key_evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["action", "narrative", "key_evidence"],
    },
}
```

`validate_output` degrade shape: `{"action": "needs_human", "narrative": f"Agent output rejected ({reason}); needs investigation.", "key_evidence": out.get("key_evidence", [])}` — and the returned dict carries `"contract_violation": reason` for the caller's audit payload. `apply_agent_proposal` mirrors `plan_run`'s supersede-then-insert but scoped to ONE row: `UPDATE ... SET status='superseded' WHERE id=:id AND status='proposed' RETURNING id` — zero rows → return False; then insert the agent row; `await db.commit()`.

- [ ] **Step 4: Run the file — PASS. Regression: `tests/test_resolution_planner.py tests/test_resolution_plan_run.py -q`.**
- [ ] **Step 5: Commit** `feat(recon): ResolutionAgent core — single-call classify, contract validation, supersede-apply`

---

### Task 5: Celery task + dispatch after planning

**Files:**
- Create: `backend/app/workers/tasks/recon_resolution_agent.py`
- Modify: `backend/app/workers/tasks/__init__.py` (import/register — mirror how recon_envelope_dry_run is exposed; check the actual registration pattern)
- Modify: `backend/app/services/reconciliation/order_recon_job.py` (dispatch after plan_run success in the hook)
- Modify: `backend/app/api/v1/reconciliation.py` (`plan_resolutions` dispatches after successful plan when flag on)
- Test: `backend/tests/test_resolution_agent_task.py`

**Interfaces:**
- Task `tasks.recon_resolution_agent` (queue `recon`, `base=InstrumentedTask`), kwargs `tenant_id: str, run_id: str`. Flow: flag gates (`reconciliation` AND `recon_resolution_agent`, mirroring `recon_envelope_dry_run.py`'s `is_enabled` checks) → `worker_async_session()` + `set_tenant_context` → resolve LLM via `get_tenant_ai_config(db, tenant_id)` (import from where the orchestrator does — check `backend/app/services/chat/orchestrator.py:2272`) + `get_adapter(provider, api_key)` → `fetch_agent_eligible` → per item: `asyncio.wait_for(classify_item(...), PER_ITEM_TIMEOUT_SECONDS)` inside try/except; ANY exception → treat as needs_human output with note, continue → `validate_output` → `apply_agent_proposal` → every 10 items update its Job row `result_summary={"processed": i, "total": n}` (job id available via `self._job_id`; use the same session pattern InstrumentedTask uses — a short separate sync session via `tenant_session`, matching `base_task.py`). Final summary dict `{processed, upgraded, kept_needs_human, contract_violations}` (returned → InstrumentedTask stores it).
- Dispatch helper `def dispatch_resolution_agent(tenant_id, run_id) -> None` in the task module: `celery_app.send_task("tasks.recon_resolution_agent", kwargs={...}, queue="recon")` — called (a) in the OrderReconJob hook right after a successful `plan_run` (inside the same try, flag-checked via `feature_flag_service.is_enabled` before sending), (b) in `plan_resolutions` after a successful plan (same flag check). Fire-and-forget; failures to enqueue log a warning, never fail the caller.

- [ ] **Step 1: Failing tests:** (a) task function processes a seeded run end-to-end with the FakeAdapter monkeypatched over `get_adapter` and a monkeypatched `get_tenant_ai_config` (returns ("anthropic", "test-model", "sk-test", False)) → planner needs_human row becomes superseded + agent row exists; (b) flag off → task returns `{"skipped": "flag_disabled"}` and touches nothing; (c) `plan_resolutions` dispatches (monkeypatch `dispatch_resolution_agent`, assert called once) only when the agent flag is on; (d) hook dispatch: monkeypatch in `order_recon_job`'s namespace, drive the hook path like `test_resolution_plan_hook.py` does, assert called when flag on / not when off.
- [ ] **Step 2: RED → Step 3: implement → Step 4: run file + `tests/test_resolution_plan_hook.py` regression → Step 5: Commit** `feat(recon): resolution-agent Celery task + flag-gated dispatch after planning`

---

### Task 6: Extract shared group-approve core (service) — endpoint refactor, behavior-identical

**Files:**
- Create: `backend/app/services/reconciliation/group_actions.py`
- Modify: `backend/app/api/v1/reconciliation.py` (`approve_resolution_group` delegates)
- Test: existing `backend/tests/test_resolution_group_actions.py` must pass UNCHANGED (that is the acceptance test); add `backend/tests/test_group_actions_core.py` with one direct-service-call test.

**Interfaces:** `async def approve_group_core(db, *, tenant_id, actor_id, run_id: str, group_key: str, notes, included_above_materiality_ids, excluded_ids, currency) -> ResolutionGroupApproveResult` — the ENTIRE body of today's endpoint (UI-flag check, run guard via `_get_run_or_404`/`_ensure_run_open` equivalents moved or imported, group parse, eligibility UPDATE, result flip, audit, commit). The endpoint becomes a thin wrapper passing `user.tenant_id`/`user.id`. Move `_parse_group_key` into the service; keep API-module names importable (`from app.services.reconciliation.group_actions import ...`) so nothing else breaks. HTTPException stays the error mechanism (the chat tool will catch it and shape a tool error).

- [ ] Steps: failing direct-call test first (approve via `approve_group_core` with no FastAPI user object) → move code → endpoint delegates → run `tests/test_resolution_group_actions.py tests/test_close_carried_forward.py tests/test_resolution_summary_api.py -q` (must be green UNCHANGED) → commit `refactor(recon): extract approve_group_core service (endpoint behavior-identical)`

---

### Task 7: Chat tools — `recon.get_resolution_summary` + `recon.approve_group` (HITL card)

**Files:**
- Create: `backend/app/mcp/tools/recon_resolution_summary.py`, `backend/app/mcp/tools/recon_approve_group.py`
- Modify: `backend/app/mcp/registry.py` (imports + two entries), `backend/app/mcp/governance.py` (TOOL_CONFIGS for `recon.approve_group`: `timeout_seconds: 30, rate_limit_per_minute: 10, requires_entitlement: "mcp_tools", allowlisted_params: ["run_id", "group_key", "currency", "notes"]` — mirror `recon.run`'s shape)
- Modify: `backend/app/services/chat/agents/base_agent.py` (intercept branch for `recon_approve_group` BEFORE execute, modeled on the mutation intercept at ~1234; note LLM-facing name is the sanitized underscore form)
- Create: `build_recon_group_confirmation(...)` in `backend/app/services/chat/write_confirmation_service.py` (small builder reusing `generate_confirmation_token` with the DEFAULT event_type so the orchestrator's existing `validate_and_extract_confirmation` approve path works unchanged; payload `type="write_confirmation"`, `mutation_type="update"`, `record_type="reconciliation group"`, `record_id=group_key`, `proposed_fields={"group": group_key, "currency": ..., "notes": ..., "items": "<count unknown at card time — omit>"}`, `tool_name="recon.approve_group"`, `tool_input={run_id, group_key, currency, notes}`)
- Modify: `backend/tests/test_prompt_tool_sync.py` (`_all_known_tool_names_for_tenant_with_every_connector` gains `recon_get_resolution_summary`, `recon_approve_group` — ONLY if these tools surface in the default chat tool schema; check how recon.get_exceptions reaches `build_all_tool_definitions` and mirror exactly; if recon tools are feature/flag-gated into the schema, follow that gate)
- Test: `backend/tests/test_recon_resolution_chat_tools.py`

**Interfaces & behavior:**
- `recon.get_resolution_summary` execute(params={run_id}, **kwargs): both-convention context handling (copy the recon_exceptions.py:88-99 block verbatim); returns `{success, run_id, match_rate, explained_rate, proposals_count, groups: [≤20, ordered amount desc, each {group_key, currency, root_cause, action, booking_vehicle, count, proposed_count, approved_count, total_amount(str), above_materiality_count}], group_count, truncated}` — every amount `str()`, description instructs VERBATIM transcription (copy the honest-framing language style from recon.get_exceptions' registry description).
- `recon.approve_group` execute(): NEVER approves directly when reached through chat streaming — the base_agent intercept fires first and yields the confirmation card. The execute function IS the post-approval path (orchestrator `validate_and_extract_confirmation` → `execute_tool_call("recon.approve_group", tool_input)`): it calls `approve_group_core` (Task 6) and shapes `{success, approved_count, skipped_count, correlation_id}` or `{success: False, error}` from HTTPException detail. It must read context both-convention and derive `actor_id` from context.
- base_agent intercept (place adjacent to the mutation intercept; keep it small):

```python
                    # ── Recon group-approve HITL intercept (Phase 2) ──
                    if block.name == "recon_approve_group":
                        from app.services.chat.write_confirmation_service import (
                            build_recon_group_confirmation,
                        )

                        payload = build_recon_group_confirmation(
                            tool_input=block.input,
                            session_id=session_id if session_id else str(self.tenant_id),
                        )
                        yield ("confirmation_required", payload.model_dump())
                        result_str = json.dumps(
                            {
                                "confirmation_required": True,
                                "message": (
                                    "Approving this resolution group requires human confirmation. "
                                    "The confirmation card has been shown. Do NOT proceed until "
                                    "the user explicitly approves."
                                ),
                            }
                        )
                        # mirror the tool_end/log/tool_result bookkeeping of the
                        # mutation intercept above (same fields), then `continue`
```

- [ ] **Step 1: Failing tests:** (a) summary tool happy path + missing-context error + truncation honesty (seed >20 groups? impractical — assert `truncated` False and `group_count == len(groups)` on a small run, plus a unit-level cap check by monkeypatching the cap to 1); (b) approve tool executes via `approve_group_core` and approves the seeded fee group (both-convention kwargs); (c) approve tool surfaces HTTPException(403) from the core as `{"success": False, "error": ...}` when `recon_resolution_ui` flag off; (d) token round-trip: `build_recon_group_confirmation` payload → `validate_and_extract_confirmation(payload.model_dump() | {"status": "pending", "type": "write_confirmation"}, session_id)` returns `(True, "recon.approve_group", tool_input)`; tampered `tool_input` → False. (e) `test_prompt_tool_sync.py` still green after registry additions.
- [ ] **Step 2-4: RED → implement → run new file + `tests/test_prompt_tool_sync.py` + `tests/test_recon_tools_dispatch.py` — PASS.**
- [ ] **Step 5: Commit** `feat(recon): chat tools — resolution summary (read) + group approve behind HITL confirmation card`

---

### Task 8: Agent progress in summary payload + FE

**Files:**
- Modify: `backend/app/api/v1/reconciliation.py` (`get_resolution_summary` gains `agent_job`), `backend/app/schemas/reconciliation.py` (`ResolutionSummaryResponse.agent_job: AgentJobStatus | None = None`; `class AgentJobStatus(BaseModel): status: str; processed: int = 0; total: int = 0`)
- Modify: `frontend/src/lib/types.ts` (mirror `agent_job`), `frontend/src/hooks/use-resolution.ts` (`useResolutionSummary` gains `refetchInterval: (q) => q.state.data?.agent_job?.status === "running" ? 5000 : false`), `frontend/src/components/reconciliation/resolution-summary-header.tsx` (running badge: "Agent investigating… {processed}/{total}")
- Test: extend `backend/tests/test_resolution_summary_api.py` (seed a Job row `job_type="tasks.recon_resolution_agent"`, `parameters={"run_id": str(run.id), ...}`, status running, `result_summary={"processed": 3, "total": 10}` → summary returns it; no job → `agent_job` None); FE: extend header vitest (badge renders when agent_job running).

**Backend query:** latest `Job` for the tenant where `job_type == "tasks.recon_resolution_agent"` and `parameters["run_id"].astext == run_id` (JSONB accessor — check the Job.parameters column type in `backend/app/models/` and use the matching operator), ordered `started_at desc`, limit 1. Map: running/completed/failed → status; processed/total from `result_summary` (default 0).

- [ ] Steps: RED (backend + FE tests) → implement → `npx vitest run && npx tsc --noEmit` + backend file green → commit `feat(recon): agent progress in resolution summary + FE polling badge`

---

### Task 9: Spec addendum + e2e + full regression

**Files:**
- Modify: `docs/superpowers/specs/2026-07-06-recon-summary-first-resolution-design.md` (Phase 2 addendum section: single-call agent design, `recon_resolution_agent` flag, agent action allowlist, chargeback-precedence resolution — closes ticket 86bavax8u items 1; note the deferred multi-hop/SuiteQL investigation as Phase 2.5)
- Create: `backend/tests/test_resolution_agent_e2e.py`
- Full regression + lint (backend + FE), per Global Constraints.

**e2e (seeded, FakeAdapter):** seed run with 1 manual_adjustment + 1 chargeback + 1 fee → plan → run the agent task fn directly (monkeypatched adapter/config; fake classifies the manual_adjustment as `book_fee_line` with contract-clean narrative) → assert: manual_adjustment's planner row superseded + agent row in `manual_adjustment:book_fee_line:deposit` group; chargeback row UNTOUCHED (needs_human, planner — it is not agent-eligible? it IS eligible (needs_human/planner/proposed) — fake returns needs_human for it; assert it stays needs_human with agent source? Decision: agent DOES process chargeback-rooted needs_human rows but the allowlist prevents booking actions; fake returns `needs_human` → superseded planner row replaced by agent needs_human row with enriched narrative — assert exactly that) → summary shows the new group → approve it via `approve_group_core` → results flip. Then full suites.

- [ ] Steps: RED e2e → green → spec addendum edit → full backend suite + FE suite + lint (triage: only pre-existing failures allowed; list them) → commit `test(recon): Phase 2 e2e — agent tail end-to-end + spec addendum`

---

## Post-plan gates (controller)

1. Build runs as a Workflow with an advisory `code-review-multiangle` phase on the branch diff.
2. Push both remotes; PR; blocking T2 gate (convergence criterion: stop when a round yields zero previously-unknown issues).
3. Merge → staging auto-deploy + watch; FE deploy (only Task 8 touches FE — still required); enable `recon_resolution_agent` for uat-smoke only; smoke: plan a seeded run, watch agent job, verify agent groups + zero residue cleanup.
4. Close ticket 86bavjz90; update 86bavax8u (items resolved by Tasks 1/9).

## Self-review notes

- Spec Phase 2 coverage: agent tail ✅ (T2-T5), narrative contract ✅ (T3), chat tools ✅ (T6-T7), progress UX ✅ (T8), precedence precondition ✅ (T1), spec addendum ✅ (T9). Deviation from spec detail, documented: investigation is deterministic-gather + single classification call (not multi-hop tool use) — Phase 2.5 extension noted in addendum.
- No migration; no NetSuite writes; agent budgets hard-coded constants (tenant-tunable later if needed — YAGNI).
- Type consistency: `AGENT_ALLOWED_ACTIONS`/`MAX_ITEMS_PER_RUN`/`PER_ITEM_TIMEOUT_SECONDS`/`AGENT_MAX_TOKENS` defined T2, consumed T4-T5; `approve_group_core` defined T6, consumed T7; `agent_job`/`AgentJobStatus` defined T8 backend, mirrored FE T8.
