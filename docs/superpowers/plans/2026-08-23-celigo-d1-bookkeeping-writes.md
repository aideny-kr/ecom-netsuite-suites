# Celigo D1 — Bookkeeping Writes (resolve / tag / assign)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent clear flow-error queue noise — `resolve`, `tag`, `assign` — with HITL approval, while `retry` and all bulk actions remain mechanically unreachable.

**Architecture:** `triage_flow_errors` spans bookkeeping and data-moving actions in a single tool, so tool-level allowlisting cannot separate them. D1 adds **argument-level policy** enforced at two layers, mirroring the read-only design: the tool's JSON schema is narrowed before the model sees it (so `retry` is not an offerable option), and the dispatcher re-checks arguments independently (so no caller can bypass it).

**Tech Stack:** FastAPI · Python 3.12 · pytest · Next.js 14 · Vitest

**Spec:** `docs/superpowers/specs/2026-08-23-celigo-write-capability-design.md`
**Depends on:** PR #202 merged (`celigo_tool_policy`, two-layer enforcement, mutation classification)

---

## Global Constraints

- **Worktree:** `/Users/aidenyi/projects/ecom-netsuite-suites/.claude/worktrees/celigo-writes`, branch `feat/celigo-write-capability`. Never `cd` to the main checkout.
- **pytest** (worktree has no `.venv`; cwd must be inside the *worktree's* `backend/`):
  ```bash
  cd <worktree>/backend
  /Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/<file> -v
  ```
  `asyncio_mode="auto"` is set in `pyproject.toml` — async tests need no marker.
- **Frontend:** `npx vitest run`, `npx tsc --noEmit` from `<worktree>/frontend`. `@testing-library/user-event` is NOT a dependency — use `fireEvent`.
- **E2BIG:** this repo has ~30 worktrees; if Bash fails with `E2BIG`, retry with `dangerouslyDisableSandbox: true`.
- **TDD mandatory.** Write the failing test, run it, confirm it fails *for the right reason*, then implement.
- **`retry`, `retryAll`, `resolveAll` must remain unreachable.** D1 ships zero data movement. Any diff that makes a record re-process into a destination system is out of scope and is a defect.
- **Do NOT modify `_BLOCKED_RECORD_TYPES`** — deliberate deny-list per `.claude/rules/agent-graph.md` #1.
- **Do NOT rely on the NetSuite record-type denylist for Celigo safety** (see Verified Facts).
- Audit every mutation via `audit_service.log_event()`, then `await db.commit()`.
- Commit per task. Never amend, never force-push.
- **Tier T2** — HITL invariant + MCP mutation writes. Blocking multi-angle gate pre-merge.

## Verified Facts (probed 2026-08-23 — do not re-derive, do not assume otherwise)

| Fact | Evidence |
|---|---|
| `discovered_tools` entries are `{name, description, input_schema}` | `mcp_client_service.discover_tools` maps MCP's `inputSchema` → `input_schema` |
| `build_external_tool_definitions` reads `tool.get("input_schema")` and passes it through | `chat/tools.py:169` |
| `triage_flow_errors` schema: `action` enum `[retry, resolve, tag, assign]`, plus booleans `retryAll` / `resolveAll`, plus `body`, `_id`, `_stepId` | live MCP tool schema |
| `build_confirmation_payload("update", "flowError", ...)` **builds successfully**; token is 64 chars | executed |
| The record-type gate is `mutation_guard.is_record_type_allowed`, **not** `is_safe_record_type` | `mutation_guard.py:105`. `.claude/rules/agent-graph.md` #1 names the wrong function — stale doc, fix separately |
| That gate applied to Celigo nouns: `script`/`integration` **blocked**, `flow`/`connection`/`export`/`import` **allowed** | executed. This overlap is **coincidence** — the list was built for NetSuite system records. Do not treat it as Celigo protection |
| `WriteConfirmationPayload.mutation_type` is `Literal["create","update","delete","upsert"]` | `write_confirmation_service.py:39` — a fifth verb raises at runtime |

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/services/chat/celigo_tool_policy.py` | **modify** — add argument-level policy + schema narrowing |
| `backend/app/services/chat/tools.py` | **modify** — apply narrowing (layer 1), apply arg policy (layer 2) |
| `backend/app/services/chat/agents/base_agent.py` | **modify** — supply a Celigo `record_type` to the confirmation payload |
| `frontend/src/components/chat/write-confirmation-card.tsx` | **modify** — render a Celigo triage confirmation |
| `backend/tests/test_celigo_arg_policy.py` | **new** |
| `backend/tests/test_chat_tools.py` | **modify** — narrowing + dispatcher enforcement |

---

## Task 1: Argument-level policy

**Files:**
- Modify: `backend/app/services/chat/celigo_tool_policy.py`
- Test: `backend/tests/test_celigo_arg_policy.py`

**Interfaces:**
- Consumes: existing `CELIGO_WRITE_VERBS`, `is_read_only_celigo_tool`
- Produces:
  - `CELIGO_BOOKKEEPING_ACTIONS: frozenset[str]` = `{"resolve", "tag", "assign"}`
  - `CELIGO_ARG_POLICY_TOOLS: frozenset[str]` = `{"triage_flow_errors"}`
  - `celigo_call_allowed(raw_tool_name: str, tool_input: dict) -> tuple[bool, str | None]` — `(True, None)` or `(False, reason)`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_celigo_arg_policy.py`:

```python
"""Argument-level policy — triage_flow_errors spans bookkeeping and data-moving
actions in ONE tool, so tool-level allowlisting cannot separate them."""

import pytest

from app.services.chat.celigo_tool_policy import celigo_call_allowed


class TestBookkeepingAllowed:
    @pytest.mark.parametrize("action", ["resolve", "tag", "assign"])
    def test_bookkeeping_actions_allowed(self, action):
        ok, reason = celigo_call_allowed(
            "triage_flow_errors",
            {"_id": "a" * 24, "_stepId": "b" * 24, "action": action},
        )
        assert ok is True
        assert reason is None


class TestDataMovingRefused:
    def test_retry_refused(self):
        ok, reason = celigo_call_allowed(
            "triage_flow_errors",
            {"_id": "a" * 24, "_stepId": "b" * 24, "action": "retry"},
        )
        assert ok is False
        assert "retry" in reason.lower()

    @pytest.mark.parametrize("flag", ["retryAll", "resolveAll"])
    def test_bulk_flags_refused_even_with_allowed_action(self, flag):
        """Bulk is prohibited outright — a bulk action is not meaningfully approvable."""
        ok, reason = celigo_call_allowed(
            "triage_flow_errors",
            {"_id": "a" * 24, "_stepId": "b" * 24, "action": "resolve", flag: True},
        )
        assert ok is False
        assert "bulk" in reason.lower()

    def test_bulk_flag_false_is_fine(self):
        ok, _ = celigo_call_allowed(
            "triage_flow_errors",
            {"_id": "a" * 24, "_stepId": "b" * 24, "action": "resolve", "retryAll": False},
        )
        assert ok is True


class TestFailsClosed:
    def test_missing_action_refused(self):
        ok, reason = celigo_call_allowed("triage_flow_errors", {"_id": "a" * 24})
        assert ok is False

    def test_unknown_action_refused(self):
        ok, _ = celigo_call_allowed("triage_flow_errors", {"action": "obliterate"})
        assert ok is False

    def test_non_string_action_refused(self):
        """A dict/list action must not crash or slip through."""
        for bad in [None, 123, {"a": 1}, ["resolve"]]:
            ok, _ = celigo_call_allowed("triage_flow_errors", {"action": bad})
            assert ok is False, bad

    def test_case_variant_refused(self):
        """Exact match only — 'Resolve' is not 'resolve'."""
        ok, _ = celigo_call_allowed("triage_flow_errors", {"action": "Resolve"})
        assert ok is False


class TestOtherToolsUnaffected:
    def test_read_tools_still_allowed(self):
        ok, _ = celigo_call_allowed("list_flows", {})
        assert ok is True

    def test_other_write_tools_still_refused(self):
        """D1 enables ONLY triage bookkeeping. run_flow/upsert/delete stay unreachable."""
        for tool in ["run_flow", "upsert_flow", "delete_resource", "patch_flow"]:
            ok, reason = celigo_call_allowed(tool, {})
            assert ok is False, tool
            assert reason
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd <worktree>/backend
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -m pytest tests/test_celigo_arg_policy.py -v
```
Expected: FAIL — `ImportError: cannot import name 'celigo_call_allowed'`

- [ ] **Step 3: Implement**

Add to `celigo_tool_policy.py`:

```python
# Actions on triage_flow_errors that only touch error bookkeeping — no record is
# re-processed into any destination system.
CELIGO_BOOKKEEPING_ACTIONS: frozenset[str] = frozenset({"resolve", "tag", "assign"})

# Tools whose safety depends on their ARGUMENTS, not just their name.
CELIGO_ARG_POLICY_TOOLS: frozenset[str] = frozenset({"triage_flow_errors"})

# Bulk switches are prohibited outright: a bulk action cannot be meaningfully
# reviewed by a human, so HITL approval of one would be theatre.
_BULK_FLAGS: tuple[str, ...] = ("retryAll", "resolveAll")


def celigo_call_allowed(raw_tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
    """Return (allowed, refusal_reason) for a Celigo tool call.

    Extends the name-based policy with argument inspection. ``triage_flow_errors``
    carries an ``action`` spanning bookkeeping (resolve/tag/assign) and data-moving
    (retry) operations, so the tool NAME alone cannot decide safety.

    Fails closed: an unrecognised tool, a missing action, a non-string action, or a
    case variant is refused.
    """
    if is_read_only_celigo_tool(raw_tool_name):
        return True, None

    if raw_tool_name not in CELIGO_ARG_POLICY_TOOLS:
        return False, (
            f"'{raw_tool_name}' is not available. This Celigo connection can read, and "
            f"can tidy up flow errors, but cannot create, change, run, or delete anything."
        )

    tool_input = tool_input or {}

    for flag in _BULK_FLAGS:
        if tool_input.get(flag) is True:
            return False, (
                f"Bulk error actions ({flag}) are not available — they would act on every "
                f"open error at once with no per-record review. Act on specific errors instead."
            )

    action = tool_input.get("action")
    if not isinstance(action, str) or action not in CELIGO_BOOKKEEPING_ACTIONS:
        allowed = ", ".join(sorted(CELIGO_BOOKKEEPING_ACTIONS))
        return False, (
            f"Only {allowed} are available on flow errors. Retrying an error re-processes "
            f"the record into the destination system and is not enabled."
        )

    return True, None
```

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/celigo_tool_policy.py backend/tests/test_celigo_arg_policy.py
git commit -m "feat(celigo): argument-level policy — bookkeeping error actions only, bulk prohibited"
```

---

## Task 2: Narrow the schema so `retry` is never offered

Layer 1. The model cannot choose an option it was never shown.

**Files:**
- Modify: `backend/app/services/chat/celigo_tool_policy.py` (add `narrow_celigo_tool_schema`)
- Modify: `backend/app/services/chat/tools.py` (`build_external_tool_definitions`)
- Test: `backend/tests/test_chat_tools.py`

**Interfaces:**
- Produces: `narrow_celigo_tool_schema(raw_tool_name: str, input_schema: dict) -> dict` — returns a **new** dict; must not mutate the stored `discovered_tools` entry.

- [ ] **Step 1: Write the failing test** — append to `test_chat_tools.py`:

```python
class TestCeligoSchemaNarrowing:
    class _FakeConnector:
        def __init__(self, tools):
            import uuid as _uuid
            self.id = _uuid.UUID("33333333-3333-3333-3333-333333333333")
            self.provider = "celigo_mcp"
            self.discovered_tools = tools
            self.is_enabled = True

    def _schema(self):
        return {
            "type": "object",
            "properties": {
                "_id": {"type": "string"},
                "_stepId": {"type": "string"},
                "action": {"type": "string", "enum": ["retry", "resolve", "tag", "assign"]},
                "retryAll": {"type": "boolean"},
                "resolveAll": {"type": "boolean"},
                "body": {"type": "object"},
            },
        }

    def test_retry_removed_from_action_enum(self):
        from app.services.chat.tools import build_external_tool_definitions

        c = self._FakeConnector([
            {"name": "triage_flow_errors", "description": "d", "input_schema": self._schema()}
        ])
        defs = build_external_tool_definitions([c])
        tool = next(t for t in defs if t["name"].endswith("__triage_flow_errors"))
        enum = tool["input_schema"]["properties"]["action"]["enum"]
        assert sorted(enum) == ["assign", "resolve", "tag"]
        assert "retry" not in enum

    def test_bulk_flags_removed_from_properties(self):
        from app.services.chat.tools import build_external_tool_definitions

        c = self._FakeConnector([
            {"name": "triage_flow_errors", "description": "d", "input_schema": self._schema()}
        ])
        defs = build_external_tool_definitions([c])
        props = next(
            t for t in defs if t["name"].endswith("__triage_flow_errors")
        )["input_schema"]["properties"]
        assert "retryAll" not in props
        assert "resolveAll" not in props

    def test_narrowing_does_not_mutate_stored_discovered_tools(self):
        """discovered_tools is persisted; narrowing must not corrupt the stored row."""
        from app.services.chat.tools import build_external_tool_definitions

        original = self._schema()
        c = self._FakeConnector([
            {"name": "triage_flow_errors", "description": "d", "input_schema": original}
        ])
        build_external_tool_definitions([c])
        assert "retry" in original["properties"]["action"]["enum"]
        assert "retryAll" in original["properties"]

    def test_netsuite_schemas_untouched(self):
        from app.services.chat.tools import build_external_tool_definitions

        c = self._FakeConnector([])
        c.provider = "netsuite_mcp"
        c.discovered_tools = [
            {"name": "ns_createRecord", "description": "d",
             "input_schema": {"type": "object", "properties": {"action": {"enum": ["retry"]}}}}
        ]
        defs = build_external_tool_definitions([c])
        assert defs[0]["input_schema"]["properties"]["action"]["enum"] == ["retry"]
```

- [ ] **Step 2: Run — confirm RED** (currently `retry` survives in the enum).

- [ ] **Step 3: Implement.** Add `narrow_celigo_tool_schema` to `celigo_tool_policy.py`: deep-copy the schema, intersect `properties.action.enum` with `CELIGO_BOOKKEEPING_ACTIONS`, and delete each `_BULK_FLAGS` key from `properties` (and from `required`, if present). Return the copy. Then in `build_external_tool_definitions`, when `is_celigo_provider(connector.provider)` and the tool passes the name filter, run its schema through it.

  Preserve the byte-stable sort order — `tools.py:155-158` explains the Anthropic prompt-cache breakpoint depends on it.

- [ ] **Step 4: Run — confirm GREEN**, and re-run the full `test_chat_tools.py`.

- [ ] **Step 5: Commit** — `feat(celigo): narrow triage_flow_errors schema so retry is never offered to the model`

---

## Task 3: Dispatcher enforcement (layer 2)

The choke point re-checks arguments, so no caller can bypass layer 1. Per `.claude/rules/agent-graph.md` #3.

**Files:** modify `backend/app/services/chat/tools.py` (`_execute_external_tool`); test in `test_chat_tools.py`.

- [ ] **Step 1: Failing test** — assert that calling `_execute_external_tool` directly with `{"action": "retry"}` returns an error containing "retry", that `{"action": "resolve"}` succeeds, and that **zero** calls reach the mocked `call_external_mcp_tool` on the refused path. Mirror the existing `test_layer2_dispatcher_denies_write` structure.
- [ ] **Step 2: Run — confirm RED** (the write reaches the mock).
- [ ] **Step 3: Implement** — replace the existing name-only check in `_execute_external_tool` with `celigo_call_allowed(raw_tool_name, tool_input)`, returning `{"error": reason}` on refusal. Keep the `celigo_write_blocked` warning log.
- [ ] **Step 4: GREEN**, plus the guard suites.
- [ ] **Step 5: Commit** — `feat(celigo): enforce argument-level policy at the dispatcher choke point`

---

## Task 4: HITL confirmation renders a Celigo triage

`build_confirmation_payload("update", "flowError", ...)` is **verified to build**. What's missing is a `record_type` for Celigo tools and a card that shows what will change.

**Files:** modify `backend/app/services/chat/agents/base_agent.py`; modify `frontend/src/components/chat/write-confirmation-card.tsx`; tests in both.

- [ ] **Step 1: Recon.** Read how `base_agent` derives `record_type` today (it reads a NetSuite `recordType` from tool input). Find where the confirmation payload is built (~`base_agent.py:1237-1275`) and read `write-confirmation-card.tsx` in full before changing either.
- [ ] **Step 2: Failing tests.** Backend: a `triage_flow_errors` mutation produces a payload with `record_type="flowError"` and `mutation_type="update"`. Frontend: the card renders the action, the flow id, and the affected error count for a Celigo payload.
- [ ] **Step 3: Run — confirm RED.**
- [ ] **Step 4: Implement.** Map Celigo tools to a `record_type` (`triage_flow_errors` → `"flowError"`). Keep `mutation_type` inside the existing four-verb `Literal` — `resolve`/`tag`/`assign` map to `"update"`.

  The card must state plainly **what will change and what will not**: which flow, how many errors, the action, and — because this is the whole point of D1 — that **no records will be re-sent to any system**.

  Do NOT rely on `is_record_type_allowed` for Celigo safety: its coverage of Celigo nouns is coincidental (`script`/`integration` blocked, `flow`/`connection`/`export`/`import` allowed). `celigo_call_allowed` is the real gate.
- [ ] **Step 5: GREEN** — backend suite, `npx vitest run`, `npx tsc --noEmit`.
- [ ] **Step 6: Commit** — `feat(celigo): render flow-error triage in the write confirmation card`

---

## Task 5: Regression gate

- [ ] **Step 1:** `./scripts/verify.sh` from the worktree root, backgrounded (the full run exceeds the 10-minute foreground cap). Read **verify.sh's own PASS/FAIL line** — a wrapper's exit code is not the verdict.
- [ ] **Step 2:** Confirm the baseline delta equals exactly the tests this plan added.
- [ ] **Step 3:** If `tests/api/test_metrics_api.py::test_put_refreshes_intent_embedding_on_text_change` fails, it is the known ~1/1000 `hash() % 1000` flake — see `reference_flaky_metrics_embedding_hash_collision`. Re-run; do not "fix" it here.
- [ ] **Step 4: Executable proof** that D1 shipped what it claims:

```bash
cd <worktree>/backend
/Users/aidenyi/projects/ecom-netsuite-suites/backend/.venv/bin/python -c "
from app.services.chat.celigo_tool_policy import celigo_call_allowed as ok
base = {'_id':'a'*24, '_stepId':'b'*24}
for a in ['resolve','tag','assign']:
    assert ok('triage_flow_errors', {**base,'action':a})[0], a
for a in ['retry']:
    assert not ok('triage_flow_errors', {**base,'action':a})[0], a
for f in ['retryAll','resolveAll']:
    assert not ok('triage_flow_errors', {**base,'action':'resolve',f:True})[0], f
for t in ['run_flow','upsert_flow','delete_resource']:
    assert not ok(t, {})[0], t
print('PASS: bookkeeping allowed; retry, bulk, and all other writes refused')
"
```

- [ ] **Step 5: Commit** any fixes.

---

## Self-Review

**Spec coverage.** D1 implements spec §4.1 (argument-level policy, bulk prohibited) and the D1 row of §6. Deliberately NOT in D1, and owned by D2/D3: the error catalog (§5), freshness re-read (§4.2), idempotency pre-check (§4.3), and `retry` itself.

**Placeholder scan.** None. Tasks 3 and 4 open with a "read the real code first" step rather than reproducing files I have not read in full — a deliberate instruction to inspect named files, not a vague gesture.

**Type consistency.** `celigo_call_allowed(raw_tool_name, tool_input) -> tuple[bool, str | None]` is identical in Tasks 1, 3, and 5. `CELIGO_BOOKKEEPING_ACTIONS` and `_BULK_FLAGS` are named identically in Tasks 1 and 2. `record_type="flowError"` and `mutation_type="update"` match the verified payload.

**Known imprecision, stated not hidden.** `resolve`/`tag`/`assign` map to `mutation_type="update"` because `write_confirmation_service.py:39` types it as a four-value `Literal`. They are closer to "annotate". This has no runtime effect — the verb only labels the confirmation card — but D3 will have to confront it properly for `retry`.
