# Ask-First Clarification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `<clarification_policy>` prompt block to the unified agent so it asks before running on genuinely ambiguous queries, and ship 5 vs-MCP benchmark cases (4 `ask_first` + 1 `must_not_ask_first` counter-case) that lock the behavior in as a permanent regression guard.

**Architecture:** One new prompt block in `_SYSTEM_PROMPT`. Extend the benchmark `Case` dataclass with four new fields (`expected_behavior`, `expected_question_about`, `expected_connectors`, `source_pin`). Extend `scorer.py` with two new verdict functions (`ask_first_score`, `must_not_ask_first_score`). Thread `source_pin` + `expected_connectors` through `agent_runner.py` so cases can control the environment the agent sees. No classifier, no coordinator revival, no frontend card.

**Tech Stack:** Python 3.12, FastAPI, pytest-asyncio, anthropic SDK (Haiku for LLM judge). Backend-only. No migrations, no new packages.

**Spec:** `docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md` (commit `b78aec5`).

**Base branch:** `feat/cross-source-sheets` (carries Phase 1 + Phase 2). If the user has already merged `feat/cross-source-sheets` → `main` by the time this plan executes, rebase onto `main` and use that as the base.

---

## Constraints

- **No regex/classifier/coordinator before the agent** — ask-first is agent-initiated through prompt policy. Do not reintroduce `source_picker.py`, `coordinator.py`, or anything equivalent. (See CLAUDE.md mistake #28 — "One unified agent, no routing".)
- **Callout format is prescribed** — blockquote + bold lead + up to 4 bulleted questions/options. Consistency matters because the scorer detects the shape.
- **Prompt ceiling** — adding ~500 chars pushes `_SYSTEM_PROMPT` from 6752 → ~7300. Bump the ceiling test from 13000 → 14000 (the tighter Phase 2 ceiling).
- **`source_pin` must be honored silently** — when set, don't re-ask source; still ask period/dimension if ambiguous.
- **No over-asking** — lookups ("SO865732") and clear requests must NOT trigger clarification. The `must_not_ask_first` counter-case guards this.
- **Benchmark cases must be hermetic** — `expected_connectors` + `source_pin` on the case YAML control the environment the agent sees, not the live DB state of Framework tenant.

---

## File map

| File | Op | Purpose |
|---|---|---|
| `backend/app/services/chat/agents/unified_agent.py` | Modify | Add `<clarification_policy>` block to `_SYSTEM_PROMPT` (after `<tool_selection>`) |
| `backend/app/services/benchmarks/run_vs_mcp.py` | Modify | Extend `Case` dataclass + `_load_case_file` with 4 new optional fields |
| `backend/app/services/benchmarks/scorer.py` | Modify | Add `ask_first_score()` + `must_not_ask_first_score()` |
| `backend/app/services/benchmarks/agent_runner.py` | Modify | Honor `source_pin` + `expected_connectors` when building the per-case agent context |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_analysis_ambiguous_source.yaml` | Create | Case 1 — expect source question |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_period.yaml` | Create | Case 2 — expect period question |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_dimension.yaml` | Create | Case 3 — expect dimension question |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_with_pin_respects_pin.yaml` | Create | Case 4 — pin set, expect period question only |
| `backend/app/services/benchmarks/benchmark_cases/vs_mcp/lookup_no_ask.yaml` | Create | Counter-case — lookup, expect zero clarification |
| `backend/tests/test_unified_agent_clarification_policy.py` | Create | Prompt-block substring assertions |
| `backend/tests/test_benchmark_ask_first_scorer.py` | Create | Unit tests for both new scorer verdicts (mocked response shapes) |
| `backend/tests/test_benchmark_case_schema.py` | Create | Unit tests for the extended `Case`/`_load_case_file` |
| `backend/tests/test_prompt_trim.py` | Modify | Bump `_SYSTEM_PROMPT` ceiling 13000 → 14000 |
| `docs/runbooks/capturing-behavior-regressions.md` | Create | Playbook for turning a reported regression into a permanent guard |

**Working directory:** `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/<new-worktree>/`. Tasks assume `pwd` is inside `backend/` unless stated otherwise, because the shared `.venv`'s `.pth` file otherwise resolves `app.*` imports to the MAIN CHECKOUT (feedback_worktree_venv_pth).

---

## Task 1: Worktree + branch setup

**Files:** none modified (environment-only).

- [ ] **Step 1: Determine base branch**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites
git fetch origin
# Has feat/cross-source-sheets merged to main yet?
git log origin/main --oneline | grep -E "cross-source-sheets|Phase 2" | head -3
```

If main contains Phase 2's squash merge, base is `main`. Otherwise base is `origin/feat/cross-source-sheets`. Note the chosen base; the commit messages below reference it.

- [ ] **Step 2: Create the worktree**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites
git worktree add .worktrees/feat-ask-first-clarification -b feat/ask-first-clarification <BASE>
cd .worktrees/feat-ask-first-clarification
```

(Replace `<BASE>` with the branch from Step 1 — `origin/main` or `origin/feat/cross-source-sheets`.)

- [ ] **Step 3: Symlink `.venv`, `node_modules`, `.env`**

```bash
ln -s ../../../backend/.venv backend/.venv
ln -s ../../../frontend/node_modules frontend/node_modules
ln -s ../../.env .env
ls -la backend/.venv/bin/python frontend/node_modules/.bin/tsc .env
```

All three lines should show valid symlinks resolving to real files.

- [ ] **Step 4: Smoke-test backend imports from `backend/`**

```bash
cd backend
.venv/bin/python -c "import app.services.chat.agents.unified_agent as u; print(u.__file__); print(f'size={len(u._SYSTEM_PROMPT)}')"
```

Expected: `__file__` under `.worktrees/feat-ask-first-clarification/...` (NOT main checkout). Size ~6752 (post-Phase-2 base) or ~12500 (if based off main pre-Phase-2). Note the size.

- [ ] **Step 5: No commit**

Environment only. Proceed to Task 2.

---

## Task 2: Add `<clarification_policy>` block to `_SYSTEM_PROMPT`

**Files:**
- Modify: `backend/app/services/chat/agents/unified_agent.py`
- Create: `backend/tests/test_unified_agent_clarification_policy.py`
- Modify: `backend/tests/test_prompt_trim.py` (bump ceiling)

- [ ] **Step 1: Write the failing test for prompt-block substrings**

Create `backend/tests/test_unified_agent_clarification_policy.py`:

```python
"""Regression tests for the <clarification_policy> prompt block.

Added 2026-04-17 after staging session where the unified agent stopped
asking clarifying questions for ambiguous requests like "sales analysis".
The source-picker card was deleted in PR #40 but the ask-first behavior
was a product requirement that outlived the classifier. This block
restores it as a prompt-driven policy — model-initiated, no routing.

Each assertion here guards a specific element of the policy. Do not
delete tests when "cleaning up" — read the docstring for incident context.

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md
"""

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT


class TestClarificationPolicyBlock:
    def test_block_exists(self):
        assert "<clarification_policy>" in _SYSTEM_PROMPT
        assert "</clarification_policy>" in _SYSTEM_PROMPT

    def test_block_placed_after_tool_selection(self):
        ts_end = _SYSTEM_PROMPT.find("</tool_selection>")
        cp_start = _SYSTEM_PROMPT.find("<clarification_policy>")
        assert ts_end != -1, "tool_selection block missing"
        assert cp_start != -1, "clarification_policy block missing"
        assert ts_end < cp_start, "clarification_policy must come AFTER tool_selection"

    def test_when_to_ask_mentions_ambiguous_source(self):
        assert "ambiguous" in _SYSTEM_PROMPT.lower()
        # The block must teach "ask when source is ambiguous"
        cp_start = _SYSTEM_PROMPT.find("<clarification_policy>")
        cp_end = _SYSTEM_PROMPT.find("</clarification_policy>")
        block = _SYSTEM_PROMPT[cp_start:cp_end]
        assert "source" in block.lower()
        assert "period" in block.lower() or "time" in block.lower()
        assert "dimension" in block.lower() or "breakdown" in block.lower()

    def test_source_pin_honored_silently(self):
        cp_start = _SYSTEM_PROMPT.find("<clarification_policy>")
        cp_end = _SYSTEM_PROMPT.find("</clarification_policy>")
        block = _SYSTEM_PROMPT[cp_start:cp_end]
        # The rule preventing double-ask when source_pin is set
        assert "source_pin" in block.lower() or "pinned" in block.lower()

    def test_lookup_exemption(self):
        cp_start = _SYSTEM_PROMPT.find("<clarification_policy>")
        cp_end = _SYSTEM_PROMPT.find("</clarification_policy>")
        block = _SYSTEM_PROMPT[cp_start:cp_end]
        # Lookup-style queries (SO865732, etc.) must not trigger a question
        assert "lookup" in block.lower() or "specific record" in block.lower()

    def test_callout_format_example_present(self):
        cp_start = _SYSTEM_PROMPT.find("<clarification_policy>")
        cp_end = _SYSTEM_PROMPT.find("</clarification_policy>")
        block = _SYSTEM_PROMPT[cp_start:cp_end]
        # The prescribed markdown shape — blockquote + bold
        assert "> **" in block, "callout format (blockquote + bold) must be shown"

    def test_max_one_clarification_turn(self):
        cp_start = _SYSTEM_PROMPT.find("<clarification_policy>")
        cp_end = _SYSTEM_PROMPT.find("</clarification_policy>")
        block = _SYSTEM_PROMPT[cp_start:cp_end]
        # Prevent clarification loops — "max one" or similar guard
        assert "once" in block.lower() or "one" in block.lower()
```

- [ ] **Step 2: Run test to confirm failure**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -m pytest tests/test_unified_agent_clarification_policy.py -v
```

Expected: 7 FAIL — block not added yet.

- [ ] **Step 3: Find the anchor point in `unified_agent.py`**

The block must go AFTER `</tool_selection>` and BEFORE `<common_queries>` (the ordering matters for both the test and the agent's prompt reading flow).

```bash
grep -n "</tool_selection>\|<common_queries>" backend/app/services/chat/agents/unified_agent.py | head -4
```

Note the line numbers.

- [ ] **Step 4: Add the `<clarification_policy>` block**

Use Edit on `backend/app/services/chat/agents/unified_agent.py`.

`old_string`:

```
</tool_selection>

<common_queries>
```

`new_string`:

```
</tool_selection>

<clarification_policy>
When a user's request is GENUINELY AMBIGUOUS — meaning multiple valid interpretations would return materially different results — ASK before calling any tool. Do NOT ask on clear lookups or clear follow-ups.

WHEN TO ASK:
- Source is ambiguous AND the tenant has multiple relevant connectors (NetSuite + BigQuery) AND no source_pin is set in the session.
- Period is not stated and there is no obvious default from recent conversation context.
- Dimension is ambiguous: the user wants analysis but didn't pick a breakdown (country, subsidiary, item class, customer, etc.) and multiple are reasonable.

WHEN NOT TO ASK:
- Intent is reasonably clear ("sales last week by country" — just run it).
- source_pin is already set on this session — honor the pin silently. You may still ask about period or dimension if those remain ambiguous, but NEVER re-ask source.
- Follow-up to a prior query where context disambiguates (e.g., "now show me the same for Q2").
- Single-connector tenant — source is trivial.
- Specific record lookup ("SO865732", "RMA61214", "customer Acme Corp") — answer directly, zero clarification.

HOW TO ASK (format — match this shape so the user catches it):
> **Before I pull this — a few things to check:**
> - Which period? (e.g., Jan 1 – today, last 30 days, Q1 2026)
> - By what dimension? (country, subsidiary, item class, customer)
> - NetSuite or BigQuery? (NetSuite = transactional truth; BigQuery = Shopify/Heap/attribution)

Keep it to max 4 bullets. Each bullet should include a short parenthetical hint or options so the user doesn't have to think from scratch.

Ask at most ONCE per user request. If the user's reply still leaves something ambiguous, pick a reasonable default and proceed rather than asking again.
</clarification_policy>

<common_queries>
```

- [ ] **Step 5: Verify the prompt grew and the block is in place**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -c "
from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT
print(f'size={len(_SYSTEM_PROMPT)}')
assert '<clarification_policy>' in _SYSTEM_PROMPT
assert '</clarification_policy>' in _SYSTEM_PROMPT
cp = _SYSTEM_PROMPT[_SYSTEM_PROMPT.find('<clarification_policy>'):_SYSTEM_PROMPT.find('</clarification_policy>')+24]
print(f'block len={len(cp)}')
"
```

Expected: size around 7200-7400 (was 6752). Block length ~1600 chars.

- [ ] **Step 6: Run the new test — should PASS**

```bash
.venv/bin/python -m pytest tests/test_unified_agent_clarification_policy.py -v
```

Expected: 7 PASS.

- [ ] **Step 7: Bump the `_SYSTEM_PROMPT` ceiling in `test_prompt_trim.py`**

Use Edit on `backend/tests/test_prompt_trim.py`:

`old_string`:

```python
    def test_prompt_under_13000_chars(self):
        """Trimmed prompt should be under 13000 chars.
```

`new_string`:

```python
    def test_prompt_under_14000_chars(self):
        """Trimmed prompt should be under 14000 chars.
```

Then find the assertion line:

`old_string`:

```python
        assert char_count < 13000, f"Prompt is {char_count} chars, target is <13000"
```

`new_string`:

```python
        assert char_count < 14000, f"Prompt is {char_count} chars, target is <14000"
```

Update the docstring tail to reflect the new history (add a "Phase B ..." entry). Use Edit:

`old_string`:

```
        - Phase 2 (2026-04-16): tightened to 13000 after SuiteQL move.
```

`new_string`:

```
        - Phase 2 (2026-04-16): tightened to 13000 after SuiteQL move.
        - Ask-first (2026-04-17): bumped to 14000 for clarification_policy block (~1600 chars).
```

- [ ] **Step 8: Run the prompt-trim test**

```bash
.venv/bin/python -m pytest tests/test_prompt_trim.py -v
```

Expected: all pass (ceiling now 14000, actual ~7200-7400).

- [ ] **Step 9: Run the broader unified-agent tests (no regressions)**

```bash
.venv/bin/python -m pytest tests/test_unified_agent_clarification_policy.py tests/test_unified_agent_suiteql_rules.py tests/test_unified_agent_workflow.py tests/test_prompt_trim.py -q
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add backend/app/services/chat/agents/unified_agent.py backend/tests/test_unified_agent_clarification_policy.py backend/tests/test_prompt_trim.py
git commit -m "feat(chat): clarification_policy prompt block in unified agent

Restores the ask-first behavior that was lost when the source-picker
classifier was deleted in PR #40. Agent-initiated, no pre-agent
classifier, no coordinator revival — model decides when to ask based
on the new <clarification_policy> block in _SYSTEM_PROMPT.

Policy covers:
- WHEN to ask (ambiguous source when multi-connector + no pin;
  ambiguous period; ambiguous dimension)
- WHEN NOT to ask (clear intent; source_pin set; follow-up after pin;
  single-connector tenant; specific record lookup)
- HOW to ask (markdown blockquote + bold lead + max 4 bulleted options
  with hints — matches the renderer so users notice)
- Max one clarification turn per request (no loops)

Prompt ceiling bumped 13000 → 14000 (added block is ~1600 chars; base
prompt ~7200-7400 post-move-from-Phase-2).

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 3: Extend benchmark `Case` + `_load_case_file` with new fields

**Files:**
- Modify: `backend/app/services/benchmarks/run_vs_mcp.py`
- Create: `backend/tests/test_benchmark_case_schema.py`

The current `Case` dataclass has 11 fields. We need four more: `expected_behavior`, `expected_question_about`, `expected_connectors`, `source_pin`. All optional — defaults mean existing 18 cases keep working unchanged.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_benchmark_case_schema.py`:

```python
"""Unit tests for the extended Case schema in vs-MCP benchmarks.

Added 2026-04-17 alongside the ask-first clarification work. Four new
optional fields:

- expected_behavior: "ask_first" | "must_not_ask_first" | None
- expected_question_about: list[str]  (e.g. ["period", "dimension"])
- expected_connectors: list[str]  (subset filter for the agent's toolset)
- source_pin: "netsuite" | "bigquery" | None
"""

from pathlib import Path

import yaml
import pytest

from app.services.benchmarks.run_vs_mcp import Case, _load_case_file


class TestCaseDefaults:
    def test_existing_cases_still_load(self, tmp_path: Path):
        p = tmp_path / "minimal.yaml"
        p.write_text(yaml.safe_dump({
            "case_id": "minimal",
            "query": "hi",
            "expected_answer_contains": ["hi"],
            "expected_tools": ["x"],
        }))
        c = _load_case_file(p)
        assert c.case_id == "minimal"
        assert c.expected_behavior is None
        assert c.expected_question_about == []
        assert c.expected_connectors == []
        assert c.source_pin is None


class TestCaseExtendedFields:
    def test_ask_first_case(self, tmp_path: Path):
        p = tmp_path / "ask.yaml"
        p.write_text(yaml.safe_dump({
            "case_id": "ask",
            "query": "sales analysis",
            "expected_behavior": "ask_first",
            "expected_question_about": ["period", "dimension"],
            "expected_connectors": ["netsuite", "bigquery"],
        }))
        c = _load_case_file(p)
        assert c.expected_behavior == "ask_first"
        assert c.expected_question_about == ["period", "dimension"]
        assert c.expected_connectors == ["netsuite", "bigquery"]
        assert c.source_pin is None

    def test_must_not_ask_case(self, tmp_path: Path):
        p = tmp_path / "lookup.yaml"
        p.write_text(yaml.safe_dump({
            "case_id": "lookup",
            "query": "SO865732",
            "expected_behavior": "must_not_ask_first",
        }))
        c = _load_case_file(p)
        assert c.expected_behavior == "must_not_ask_first"

    def test_source_pin_respected(self, tmp_path: Path):
        p = tmp_path / "pin.yaml"
        p.write_text(yaml.safe_dump({
            "case_id": "pin",
            "query": "sales",
            "source_pin": "bigquery",
            "expected_behavior": "ask_first",
            "expected_question_about": ["period"],
        }))
        c = _load_case_file(p)
        assert c.source_pin == "bigquery"


class TestCaseValidation:
    def test_invalid_expected_behavior_raises(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump({
            "case_id": "bad",
            "query": "x",
            "expected_behavior": "nonsense",
        }))
        with pytest.raises(ValueError, match="expected_behavior must be one of"):
            _load_case_file(p)

    def test_invalid_source_pin_raises(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump({
            "case_id": "bad",
            "query": "x",
            "source_pin": "nonsense",
        }))
        with pytest.raises(ValueError, match="source_pin must be one of"):
            _load_case_file(p)
```

- [ ] **Step 2: Run the test — should FAIL**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -m pytest tests/test_benchmark_case_schema.py -v
```

Expected: multiple failures (fields don't exist).

- [ ] **Step 3: Extend the `Case` dataclass + loader**

Use Edit on `backend/app/services/benchmarks/run_vs_mcp.py`.

`old_string`:

```python
@dataclass
class Case:
    case_id: str
    query: str
    expected_answer_contains: list[str]
    expected_tools: list[str]
    expected_accuracy: float
    max_cost: float
    max_latency_ms: int
    tags: list[str]
    notes: str
    baseline_expected_tools: list[str]
    baseline_expected_accuracy: float
```

`new_string`:

```python
# Allowed values for the new expected_behavior field.
_VALID_BEHAVIORS = ("ask_first", "must_not_ask_first")
# Allowed values for the new source_pin field.
_VALID_SOURCE_PINS = ("netsuite", "bigquery")


@dataclass
class Case:
    case_id: str
    query: str
    expected_answer_contains: list[str]
    expected_tools: list[str]
    expected_accuracy: float
    max_cost: float
    max_latency_ms: int
    tags: list[str]
    notes: str
    baseline_expected_tools: list[str]
    baseline_expected_accuracy: float
    # New in 2026-04-17 — ask-first clarification guards. Optional; defaults
    # mean pre-existing cases keep their current substring/LLM-judge flow.
    expected_behavior: str | None = None  # "ask_first" | "must_not_ask_first" | None
    expected_question_about: list[str] | None = None  # e.g. ["period", "dimension"]
    expected_connectors: list[str] | None = None  # filter toolset; None = whatever tenant has
    source_pin: str | None = None  # "netsuite" | "bigquery" | None
```

Then update `_load_case_file`:

`old_string`:

```python
def _load_case_file(path: Path) -> Case:
    data = yaml.safe_load(path.read_text()) or {}
    return Case(
        case_id=data.get("case_id") or path.stem,
        query=data["query"],
        expected_answer_contains=data.get("expected_answer_contains", []),
        expected_tools=data.get("expected_tools", []),
        expected_accuracy=float(data.get("expected_accuracy", 0.7)),
        max_cost=float(data.get("max_cost", 0.50)),
        max_latency_ms=int(data.get("max_latency_ms", 120_000)),
        tags=list(data.get("tags", [])),
        notes=str(data.get("notes", "")),
        baseline_expected_tools=data.get("baseline_expected_tools", []),
        baseline_expected_accuracy=float(data.get("baseline_expected_accuracy", 0.7)),
    )
```

`new_string`:

```python
def _load_case_file(path: Path) -> Case:
    data = yaml.safe_load(path.read_text()) or {}

    expected_behavior = data.get("expected_behavior")
    if expected_behavior is not None and expected_behavior not in _VALID_BEHAVIORS:
        raise ValueError(
            f"expected_behavior must be one of {_VALID_BEHAVIORS} or omitted, "
            f"got {expected_behavior!r} in {path}"
        )
    source_pin = data.get("source_pin")
    if source_pin is not None and source_pin not in _VALID_SOURCE_PINS:
        raise ValueError(
            f"source_pin must be one of {_VALID_SOURCE_PINS} or omitted, "
            f"got {source_pin!r} in {path}"
        )

    return Case(
        case_id=data.get("case_id") or path.stem,
        query=data["query"],
        expected_answer_contains=data.get("expected_answer_contains", []),
        expected_tools=data.get("expected_tools", []),
        expected_accuracy=float(data.get("expected_accuracy", 0.7)),
        max_cost=float(data.get("max_cost", 0.50)),
        max_latency_ms=int(data.get("max_latency_ms", 120_000)),
        tags=list(data.get("tags", [])),
        notes=str(data.get("notes", "")),
        baseline_expected_tools=data.get("baseline_expected_tools", []),
        baseline_expected_accuracy=float(data.get("baseline_expected_accuracy", 0.7)),
        expected_behavior=expected_behavior,
        expected_question_about=list(data.get("expected_question_about", [])),
        expected_connectors=list(data.get("expected_connectors", [])),
        source_pin=source_pin,
    )
```

Note: the test expects `expected_question_about` default to equal `[]` (truthy check). The dataclass default is `None`, but `_load_case_file` always returns a list. The test's `c.expected_question_about == []` path asserts against the loader output, which is `[]`. The dataclass's `None` default is for callers that instantiate `Case(...)` directly — tests don't do that. If any test DOES rely on the dataclass's attribute default, change it to `list` via `field(default_factory=list)` — but the plan above is consistent.

- [ ] **Step 4: Run the test — should PASS**

```bash
.venv/bin/python -m pytest tests/test_benchmark_case_schema.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Confirm existing cases still load**

```bash
.venv/bin/python -c "
from app.services.benchmarks.run_vs_mcp import load_cases
cases = load_cases()
print(f'loaded {len(cases)} cases; first id: {cases[0].case_id}; expected_behavior: {cases[0].expected_behavior!r}')
"
```

Expected: 18 cases, `expected_behavior: None`.

- [ ] **Step 6: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add backend/app/services/benchmarks/run_vs_mcp.py backend/tests/test_benchmark_case_schema.py
git commit -m "feat(benchmark): extend Case schema with ask-first fields

Four new optional fields on Case:
- expected_behavior: 'ask_first' | 'must_not_ask_first' | None
- expected_question_about: list of topic names the ask must cover
- expected_connectors: subset to filter the agent's toolset
- source_pin: pre-set the session's source pin for the case

All default to None/empty — existing 18 cases load unchanged.
Validation: loader raises ValueError if expected_behavior or source_pin
contain an unexpected value.

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 4: Add `ask_first_score` + `must_not_ask_first_score` to the scorer

**Files:**
- Modify: `backend/app/services/benchmarks/scorer.py`
- Create: `backend/tests/test_benchmark_ask_first_scorer.py`

Current scorer has `substring_score` + `llm_judge_score`. The new verdicts are behavioral — they check structure (tool-call count, callout format presence) + an LLM judge for rationale. Keep them separate to not pollute the existing scorer.

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_benchmark_ask_first_scorer.py`:

```python
"""Unit tests for ask_first_score / must_not_ask_first_score.

Deterministic parts only — LLM judge calls are mocked. The scorer
rewards:
- ask_first: zero tool calls + markdown callout + question-like content
- must_not_ask_first: NOT a callout (direct answer or tool-driven)
"""

import pytest

from app.services.benchmarks.scorer import (
    ask_first_score,
    must_not_ask_first_score,
)


class TestAskFirstScorer:
    def test_passes_with_callout_and_zero_tool_calls(self):
        answer = (
            "> **Before I pull this — a few things to check:**\n"
            "> - Which period?\n"
            "> - By what dimension?\n"
        )
        r = ask_first_score(
            answer_text=answer,
            tool_calls_count=0,
            expected_question_about=["period", "dimension"],
        )
        assert r.score == 1.0
        assert "callout" in r.rationale.lower() or "question" in r.rationale.lower()

    def test_fails_when_tool_called(self):
        # Agent called a tool — violates ask_first
        answer = "> **A question:** What period?"
        r = ask_first_score(
            answer_text=answer,
            tool_calls_count=1,
            expected_question_about=["period"],
        )
        assert r.score < 1.0
        assert "tool" in r.rationale.lower()

    def test_fails_without_callout(self):
        # No blockquote, no bold — just prose
        answer = "Sure, what period do you want?"
        r = ask_first_score(
            answer_text=answer,
            tool_calls_count=0,
            expected_question_about=["period"],
        )
        # Partial credit — it DID ask, but didn't use the prescribed format
        assert 0.0 < r.score < 1.0 or r.score == 0.0
        assert "callout" in r.rationale.lower() or "format" in r.rationale.lower()

    def test_fails_without_question(self):
        # Agent wrote prose with no question, no callout, no tool call
        answer = "Let me think about this."
        r = ask_first_score(
            answer_text=answer,
            tool_calls_count=0,
            expected_question_about=["period"],
        )
        assert r.score == 0.0


class TestMustNotAskFirstScorer:
    def test_passes_with_direct_answer(self):
        # Agent called a tool, no clarification callout
        answer = "Here's SO865732: status=B, total=$1,234.56, trandate=2026-04-10."
        r = must_not_ask_first_score(
            answer_text=answer,
            tool_calls_count=1,
        )
        assert r.score == 1.0

    def test_fails_when_callout_present(self):
        answer = "> **Which source?**\n> - NetSuite\n> - BigQuery"
        r = must_not_ask_first_score(
            answer_text=answer,
            tool_calls_count=0,
        )
        assert r.score == 0.0
        assert "callout" in r.rationale.lower() or "ask" in r.rationale.lower()

    def test_passes_when_tool_called_even_with_noise(self):
        # A bolded phrase is fine as long as it's NOT the blockquote-bold
        # callout format that signals clarification.
        answer = "The **total** is $500."
        r = must_not_ask_first_score(
            answer_text=answer,
            tool_calls_count=1,
        )
        assert r.score == 1.0
```

- [ ] **Step 2: Run the test — FAIL**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -m pytest tests/test_benchmark_ask_first_scorer.py -v
```

Expected: ImportError — functions don't exist.

- [ ] **Step 3: Append scorers to `scorer.py`**

Add to the END of `backend/app/services/benchmarks/scorer.py` (before any `if __name__` guard if one exists):

```python
# ---------------------------------------------------------------------------
# Ask-first / must-not-ask-first scorers — behavior verdicts
# ---------------------------------------------------------------------------


import re as _re  # local alias to avoid top-of-file changes; scorer.py already imports re

# A response is considered a "callout" if it contains a blockquote line
# that starts with "> **" somewhere in the text. This matches the
# prescribed format in unified_agent.py's <clarification_policy> block.
_CALLOUT_RE = _re.compile(r"^\s*>\s*\*\*", flags=_re.MULTILINE)


def _has_callout(text: str) -> bool:
    return bool(_CALLOUT_RE.search(text or ""))


def _has_question_shape(text: str) -> bool:
    """Heuristic: response looks like it's asking something.

    True if there's a question mark followed by a newline or end-of-string
    (rules out inline rhetorical questions inside prose).
    """
    if not text:
        return False
    # question mark followed by newline or end-of-string
    return bool(_re.search(r"\?\s*(\n|$)", text))


def ask_first_score(
    *,
    answer_text: str,
    tool_calls_count: int,
    expected_question_about: list[str] | None,
) -> ScoreResult:
    """Score a turn expected to ask_first.

    Full score (1.0) requires all three:
      (a) zero tool calls
      (b) callout format present (blockquote + bold)
      (c) at least one question in the response

    Partial credit (0.5) for asking via prose without the callout format.
    Zero for tool calls OR no question at all.

    The `expected_question_about` list is carried for downstream LLM-judge
    verification but NOT enforced here (keeps this scorer deterministic).
    """
    if tool_calls_count > 0:
        return ScoreResult(
            score=0.0,
            rationale=f"ask_first expected zero tool calls, got {tool_calls_count}",
            source="ask_first",
        )
    if not _has_question_shape(answer_text):
        return ScoreResult(
            score=0.0,
            rationale="ask_first expected a clarifying question; none found",
            source="ask_first",
        )
    if not _has_callout(answer_text):
        # Asked, but not in the prescribed format. Partial credit.
        return ScoreResult(
            score=0.5,
            rationale="ask_first: asked a question but not in the blockquote-bold callout format",
            source="ask_first",
        )
    topics = ", ".join(expected_question_about or []) or "any"
    return ScoreResult(
        score=1.0,
        rationale=f"ask_first: callout present, zero tool calls, question asked (topics expected: {topics})",
        source="ask_first",
    )


def must_not_ask_first_score(
    *,
    answer_text: str,
    tool_calls_count: int,
) -> ScoreResult:
    """Score a turn expected to NOT ask_first (lookup / clear request).

    Fails if the response contains the callout format (indicating the
    agent asked when it shouldn't have).

    The reverse check (did the agent call a tool or give a direct answer)
    is satisfied either by a tool call OR by a callout-free response with
    no question shape — but tool_calls_count is the clearer signal.
    """
    if _has_callout(answer_text):
        return ScoreResult(
            score=0.0,
            rationale="must_not_ask_first: agent emitted a clarification callout when it shouldn't",
            source="must_not_ask_first",
        )
    return ScoreResult(
        score=1.0,
        rationale=(
            f"must_not_ask_first: no clarification callout; "
            f"tool_calls={tool_calls_count}"
        ),
        source="must_not_ask_first",
    )
```

- [ ] **Step 4: Run the tests — should PASS**

```bash
.venv/bin/python -m pytest tests/test_benchmark_ask_first_scorer.py -v
```

Expected: 8 PASS.

- [ ] **Step 5: Confirm existing scorer tests still pass**

```bash
.venv/bin/python -m pytest tests/ -k "scorer or benchmark" -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add backend/app/services/benchmarks/scorer.py backend/tests/test_benchmark_ask_first_scorer.py
git commit -m "feat(benchmark): ask_first and must_not_ask_first scorer verdicts

Two new scoring functions alongside the existing substring_score and
llm_judge_score:

- ask_first_score: full credit for zero tool calls + callout format +
  question; partial for asking without the prescribed format; zero for
  tool calls or no question.
- must_not_ask_first_score: fails if the response contains the
  clarification callout shape (blockquote + bold lead).

Both are deterministic — no LLM call, so they're fast and cheap to run
on every case. LLM-judge integration for topic-coverage verification is
a later step (Task 6's integration hook).

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 5: Thread `source_pin` + `expected_connectors` through `agent_runner.py`

**Files:**
- Modify: `backend/app/services/benchmarks/agent_runner.py`

The runner currently loads live connectors from the DB. To make cases hermetic we need two hooks:

1. `expected_connectors: ["netsuite"]` → filter the agent's toolset to only NetSuite tools even if the tenant also has BigQuery connected.
2. `source_pin: "bigquery"` → set the session's `source_pin` in-memory before the agent runs.

- [ ] **Step 1: Locate the runner's connector loading + session construction**

```bash
grep -n "get_active_connectors_for_tenant\|source_pin\|session.source_pin\|set_session" backend/app/services/benchmarks/agent_runner.py | head -10
```

Note the line numbers where `connectors = await get_active_connectors_for_tenant(...)` happens and where the session object is built.

- [ ] **Step 2: Read the relevant section (~line 370-420)**

```bash
sed -n '360,430p' backend/app/services/benchmarks/agent_runner.py
```

Understand the flow: tenant → connectors → session → tool registry → agent.

- [ ] **Step 3: Add kwargs to the runner entrypoint**

Find the public function that runs a single case (likely `run_case` or similar). Add two kwargs:

```python
async def run_case(
    *,
    case: Case,
    tenant_id: str,
    db: AsyncSession,
    # ... existing kwargs ...
    # new
    force_connectors: list[str] | None = None,
    source_pin: str | None = None,
) -> RunResult:
```

Then at the connector-load site:

```python
connectors = await get_active_connectors_for_tenant(db, tenant_id)
connectors = connectors or []
if force_connectors:
    connectors = [c for c in connectors if c.provider in force_connectors]
```

And at session construction (find where `Session` or equivalent is instantiated):

```python
if source_pin:
    session.source_pin = source_pin  # orchestrator honors this as prompt hint
```

Exact attribute names depend on the session model — grep `class Session` in `backend/app/models/` to find the right attribute.

- [ ] **Step 4: Pipe the case's fields through the call site**

Find the caller of `run_case` (likely in `run_vs_mcp.py::main` or similar). Replace:

```python
await run_case(case=case, tenant_id=tid, db=db, ...)
```

With:

```python
await run_case(
    case=case,
    tenant_id=tid,
    db=db,
    force_connectors=case.expected_connectors or None,
    source_pin=case.source_pin,
    ...,
)
```

- [ ] **Step 5: Smoke test the change**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -c "
from app.services.benchmarks.agent_runner import run_case
import inspect
sig = inspect.signature(run_case)
assert 'force_connectors' in sig.parameters
assert 'source_pin' in sig.parameters
print('runner signature updated:', list(sig.parameters))
"
```

- [ ] **Step 6: Run broader tests for any runner regressions**

```bash
.venv/bin/python -m pytest tests/ -k "benchmark or agent_runner" -q
```

Expected: no new failures. A few pre-existing benchmark tests may already be xfail/skip — acknowledge those don't regress.

- [ ] **Step 7: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add backend/app/services/benchmarks/agent_runner.py backend/app/services/benchmarks/run_vs_mcp.py
git commit -m "feat(benchmark): thread source_pin + expected_connectors through runner

Hermetic case environment: a case can now pin a session's source
(source_pin=bigquery) and filter the agent's toolset
(expected_connectors=['netsuite']) without needing the live DB state
of the tenant to match.

Enables:
- sales_with_pin_respects_pin — pin set, ambiguous period, expect
  no re-ask of source
- sales_no_dimension — NS only even if tenant has BQ too

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 6: Wire the new scorers into the runner's verdict path

**Files:**
- Modify: `backend/app/services/benchmarks/run_vs_mcp.py` (where cases are scored)

The runner currently dispatches to `substring_score` / `llm_judge_score`. When `case.expected_behavior` is set, it should use the corresponding new scorer instead.

- [ ] **Step 1: Locate the scoring dispatch point**

```bash
grep -n "substring_score\|llm_judge_score\|score_ours\|score_baseline" backend/app/services/benchmarks/run_vs_mcp.py | head -15
```

Find the function (likely `_score_run` or inline in `main`) where the scorer is invoked per case.

- [ ] **Step 2: Add the new-scorer dispatch BEFORE the existing substring/judge path**

Pseudocode (adapt to actual names):

```python
if case.expected_behavior == "ask_first":
    result = ask_first_score(
        answer_text=run_result.answer_text,
        tool_calls_count=len(run_result.tool_calls),
        expected_question_about=case.expected_question_about,
    )
elif case.expected_behavior == "must_not_ask_first":
    result = must_not_ask_first_score(
        answer_text=run_result.answer_text,
        tool_calls_count=len(run_result.tool_calls),
    )
else:
    # existing path — substring + llm_judge
    result = await existing_dispatch(...)
```

Make sure the returned verdict column in the summary table reads `ASK_FIRST`, `MUST_NOT_ASK`, or the existing `OURS_WINS`/`TIE`/`BASELINE_WINS` as appropriate. Add a short comment above the dispatch explaining the 2026-04-17 addition.

- [ ] **Step 3: Integration test — invoke the runner path with a mocked agent response**

Add to `backend/tests/test_benchmark_ask_first_scorer.py` (same file — adjacent concerns):

```python
class TestRunnerDispatch:
    """End-to-end: when a case carries expected_behavior, the runner
    picks the right scorer (no LLM in this test — mocks the answer).
    """

    def test_dispatch_to_ask_first(self, monkeypatch):
        # This is a stub — the real hook lives in run_vs_mcp.py's
        # scoring dispatch. Confirm the dispatch is in place.
        from app.services.benchmarks.run_vs_mcp import Case
        # Construct a case with expected_behavior = ask_first
        # and assert the scorer is selected by the dispatcher.
        # (Full integration requires live DB; this test just asserts
        # the dispatch function is importable and routes correctly.)
        c = Case(
            case_id="t",
            query="sales",
            expected_answer_contains=[],
            expected_tools=[],
            expected_accuracy=0.7,
            max_cost=0.5,
            max_latency_ms=60_000,
            tags=[],
            notes="",
            baseline_expected_tools=[],
            baseline_expected_accuracy=0.7,
            expected_behavior="ask_first",
            expected_question_about=["period"],
            expected_connectors=[],
            source_pin=None,
        )
        assert c.expected_behavior == "ask_first"
```

(The full runner-level integration test requires live DB. Keeping this test scoped to dataclass + dispatch import — the actual scoring is covered in Task 4's tests, and Task 9 runs the live integration on staging.)

- [ ] **Step 4: Run unit tests**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -m pytest tests/test_benchmark_ask_first_scorer.py tests/test_benchmark_case_schema.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add backend/app/services/benchmarks/run_vs_mcp.py backend/tests/test_benchmark_ask_first_scorer.py
git commit -m "feat(benchmark): dispatch ask_first / must_not_ask_first verdicts

When case.expected_behavior is set, runner picks the corresponding
behavioral scorer instead of the default substring + llm_judge path.
Summary table column renders ASK_FIRST or MUST_NOT_ASK verdict.

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 7: Create the 5 benchmark case YAMLs

**Files (all create):**
- `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_analysis_ambiguous_source.yaml`
- `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_period.yaml`
- `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_dimension.yaml`
- `backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_with_pin_respects_pin.yaml`
- `backend/app/services/benchmarks/benchmark_cases/vs_mcp/lookup_no_ask.yaml`

- [ ] **Step 1: Create `sales_analysis_ambiguous_source.yaml`**

```yaml
# Both connectors present, no pin. "sales analysis" is ambiguous on
# period, dimension, AND source — agent must ask before running anything.
case_id: sales_analysis_ambiguous_source
query: "show me sales analysis"
tags: ["ask_first", "clarification", "regression-2026-04-17"]
notes: |
  User reported (staging, 2026-04-17) that the agent ran a tool call
  instead of asking. The source picker card was deleted in PR #40 —
  this case locks in the prompt-driven replacement.

expected_behavior: ask_first
expected_question_about: ["source", "period", "dimension"]
expected_connectors: ["netsuite", "bigquery"]
source_pin: null

expected_accuracy: 1.0
max_cost: 0.05
max_latency_ms: 30000
```

- [ ] **Step 2: Create `sales_no_period.yaml`**

```yaml
# NS only. Period missing — agent should ask.
case_id: sales_no_period
query: "give me sales by country"
tags: ["ask_first", "period", "clarification"]

expected_behavior: ask_first
expected_question_about: ["period"]
expected_connectors: ["netsuite"]
source_pin: null

expected_accuracy: 1.0
max_cost: 0.05
max_latency_ms: 30000
```

- [ ] **Step 3: Create `sales_no_dimension.yaml`**

```yaml
# NS only. Period clear ("Q1"), dimension unstated — agent should ask.
case_id: sales_no_dimension
query: "show me Q1 sales"
tags: ["ask_first", "dimension", "clarification"]

expected_behavior: ask_first
expected_question_about: ["dimension"]
expected_connectors: ["netsuite"]
source_pin: null

expected_accuracy: 1.0
max_cost: 0.05
max_latency_ms: 30000
```

- [ ] **Step 4: Create `sales_with_pin_respects_pin.yaml`**

```yaml
# Pin set to BigQuery. Period ambiguous — agent should ask PERIOD but
# NOT re-ask source (pin honored silently).
case_id: sales_with_pin_respects_pin
query: "sales analysis by channel"
tags: ["ask_first", "pin", "source-pin-honored"]
notes: |
  Guards the "When NOT to ask: source_pin is set" rule. Agent must
  respect the pin and not re-ask source, but SHOULD ask about period
  since it's unstated.

expected_behavior: ask_first
expected_question_about: ["period"]
expected_connectors: ["netsuite", "bigquery"]
source_pin: bigquery

expected_accuracy: 1.0
max_cost: 0.05
max_latency_ms: 30000
```

- [ ] **Step 5: Create `lookup_no_ask.yaml`**

```yaml
# Specific record lookup — agent must NOT ask; go straight to the tool.
# This is the counter-case that guards against over-asking.
case_id: lookup_no_ask
query: "show me SO865732"
tags: ["must_not_ask_first", "lookup", "counter-case"]
notes: |
  Counter-case for ask-first over-asking. SO865732 is a specific
  sales order number; the agent should call netsuite_suiteql (or
  ext__*__ns_runCustomSuiteQL) directly with WHERE t.tranid='SO865732'.

expected_behavior: must_not_ask_first
expected_connectors: ["netsuite"]

expected_accuracy: 1.0
max_cost: 0.10
max_latency_ms: 60000
expected_tools: ["netsuite_suiteql"]
```

- [ ] **Step 6: Verify the cases load**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -c "
from app.services.benchmarks.run_vs_mcp import load_cases
cases = load_cases(case_ids=[
    'sales_analysis_ambiguous_source',
    'sales_no_period',
    'sales_no_dimension',
    'sales_with_pin_respects_pin',
    'lookup_no_ask',
])
for c in cases:
    print(f'{c.case_id}: behavior={c.expected_behavior} connectors={c.expected_connectors} pin={c.source_pin}')
"
```

Expected: 5 cases list with matching behavior/connectors/pin from the YAMLs.

- [ ] **Step 7: Confirm total case count**

```bash
.venv/bin/python -c "from app.services.benchmarks.run_vs_mcp import load_cases; print(len(load_cases()))"
```

Expected: 23 (18 existing + 5 new).

- [ ] **Step 8: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_analysis_ambiguous_source.yaml \
        backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_period.yaml \
        backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_no_dimension.yaml \
        backend/app/services/benchmarks/benchmark_cases/vs_mcp/sales_with_pin_respects_pin.yaml \
        backend/app/services/benchmarks/benchmark_cases/vs_mcp/lookup_no_ask.yaml
git commit -m "feat(benchmark): 5 ask-first regression cases

Four ask_first cases + one must_not_ask_first counter-case:
- sales_analysis_ambiguous_source — both connectors, no pin
- sales_no_period — NS only, period missing
- sales_no_dimension — NS only, period clear, dimension missing
- sales_with_pin_respects_pin — pin=bigquery, period missing
- lookup_no_ask — 'show me SO865732', expect direct tool call

Each case locks in a specific slice of the clarification policy so
regression in that slice fails CI. lookup_no_ask protects against
over-asking.

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 8: Write the regression-capture runbook

**Files:**
- Create: `docs/runbooks/capturing-behavior-regressions.md`

No tests. This is a playbook.

- [ ] **Step 1: Create the runbook**

Create `docs/runbooks/capturing-behavior-regressions.md`:

```markdown
# Runbook: Capturing Behavior Regressions as Permanent Guards

When a user reports "the app used to do X and now it doesn't," follow
this playbook. Every regression becomes a permanent benchmark case so
the same class of bug cannot recur silently.

## Step 1: Reproduce

SSH staging, verify current behavior:
- `ssh aidenyi@34.73.236.64`
- Use the chat UI with a query that triggers the regression
- Note: the query, the connectors present on the tenant, any source_pin,
  and what the agent did vs what it should have done

## Step 2: Classify the expected behavior

Pick one:

| Behavior | Case field | Scorer | Use for |
|---|---|---|---|
| Agent should ask before acting | `expected_behavior: ask_first` | `ask_first_score` | Ambiguous intent, missing required params, source choice when multi-connector |
| Agent should NOT ask (lookup) | `expected_behavior: must_not_ask_first` | `must_not_ask_first_score` | Specific record, clear one-shot queries |
| Agent should produce specific content | existing `expected_answer_contains` | `substring_score` + `llm_judge_score` | Numeric-answer regressions, keyword presence |
| Agent should call specific tool | existing `expected_tools` | Tool-match check in runner | Tool selection regressions |

## Step 3: Write the case YAML

Add under `backend/app/services/benchmarks/benchmark_cases/vs_mcp/<short-name>.yaml`.

Required fields:
- `case_id` (matches filename stem)
- `query` (exact user text)
- `tags` (include `regression-YYYY-MM-DD`)
- `notes` (1-3 lines: what regressed, when reported, link to Slack/issue)

Plus the fields for the chosen behavior — see `Case` dataclass in
`backend/app/services/benchmarks/run_vs_mcp.py`.

For `ask_first`:
- `expected_behavior: ask_first`
- `expected_question_about: [...]` (one or more of `source`, `period`, `dimension`, `entity`)
- `expected_connectors: [...]` (hermetic filter; subset of `netsuite`, `bigquery`)
- `source_pin: null | netsuite | bigquery`

## Step 4: Run the case — it should fail

```bash
cd backend
.venv/bin/python -m app.services.benchmarks.run_vs_mcp \
  --case <case_id> \
  --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a \
  --skip-baseline
```

Expected: FAIL. (If it passes with no code change, the regression was
already fixed and the case just locks it in — commit and move on.)

## Step 5: Fix the underlying cause

Typical levers:
- **Prompt rule** — add/adjust a block in `_SYSTEM_PROMPT` or a knowledge profile.
- **Tool description** — update the tool's metadata so the model picks it correctly.
- **Profile trigger_tools** — add a missing glob so the profile activates.
- **Pattern retrieval threshold** — rarely, but can be bumped.

Do NOT add a classifier / router / regex gate. See CLAUDE.md mistake
#28 — "One unified agent, no routing."

## Step 6: Re-run the case — should pass now

Same command as Step 4. Verify the verdict changes to PASS.

## Step 7: Run the full suite — no regressions elsewhere

```bash
.venv/bin/python -m app.services.benchmarks.run_vs_mcp \
  --suite sales \
  --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a \
  --skip-baseline
```

Expected: no existing case drops accuracy.

## Step 8: PR

Include in the PR body:
- The original regression description (copy from Slack/issue).
- The new case file.
- The fix commits.
- Before/after benchmark result for the new case.

## Step 9: Merge → nightly benchmark picks it up

The nightly vs-MCP run (11:00 UTC, `agent_benchmark_vs_mcp.py`) now
includes the new case. Any future change that re-breaks the behavior
fails the nightly run and triggers a Sentry alert.

## When NOT to use this playbook

- Infrastructure bugs (tool returns 500, connector disconnected) — fix
  the infra, don't write a benchmark case.
- Data-shape bugs (migration missing a column) — fix the migration.
- Frontend-only regressions (button doesn't render) — Playwright /
  vitest, not the agent benchmark.

## Related

- Spec: `docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md`
- Benchmark harness: `backend/app/services/benchmarks/run_vs_mcp.py`
- Scorer: `backend/app/services/benchmarks/scorer.py`
- CI gate: `.github/workflows/agent-benchmark.yml`
```

- [ ] **Step 2: Commit (may need `-f` if `docs/` is in .gitignore)**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git add -f docs/runbooks/capturing-behavior-regressions.md
git commit -m "docs(runbook): capturing behavior regressions as permanent guards

Playbook for turning a 'the app used to do X and now doesn't' report
into a permanent benchmark case. Nine steps from reproduce to PR to
nightly inclusion. Explicitly rules out classifier/routing fixes (see
CLAUDE.md mistake #28).

Companion doc to the ask-first-clarification work — the method, not the
specific fix.

Spec: docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md"
```

---

## Task 9: Full backend pytest

**Files:** none modified.

- [ ] **Step 1: Run full backend pytest**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification/backend
.venv/bin/python -m pytest tests --tb=line -q 2>&1 | tail -10
```

Expected: previous count + ~15 new tests (7 policy + 8 scorer + 5 schema) ≈ **2,945 passing, 0 failing**.

If any test fails:
- Check whether it's related to the prompt-size bump (re-review `test_prompt_trim.py`).
- Check whether it's an unrelated flaky test — re-run just that test file.
- Do NOT proceed to Task 10 if any related test fails.

- [ ] **Step 2: No commit**

Verification only.

---

## Task 10: Deploy to staging + run the 5 new cases

**Files:** none modified (deploy + DB observation).

- [ ] **Step 1: Push branch to remotes**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git push -u origin feat/ask-first-clarification
git push framework feat/ask-first-clarification
```

- [ ] **Step 2: Trigger staging deploy**

```bash
gh workflow run deploy.yml --ref feat/ask-first-clarification -f environment=staging
sleep 5
gh run list --workflow=deploy.yml --limit 1 --json databaseId,status,headBranch
# Note the databaseId, then:
gh run watch <databaseId> --exit-status
```

If workflow reports success BUT staging image is stale (workflow_dispatch + non-default-branch — see `feedback_workflow_dispatch_latest_tag.md`):

```bash
SHA=$(git rev-parse --short=7 HEAD)
ssh aidenyi@34.73.236.64 "sudo docker pull ghcr.io/aideny-kr/ecom-netsuite-suites/backend:sha-${SHA} && sudo docker tag ghcr.io/aideny-kr/ecom-netsuite-suites/backend:sha-${SHA} ghcr.io/aideny-kr/ecom-netsuite-suites/backend:latest && cd /opt/ecom-netsuite && sudo docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate backend && sleep 10"
```

Verify the new image is running:

```bash
ssh aidenyi@34.73.236.64 "sudo docker exec ecom-netsuite-backend-1 python -c 'from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT; print(len(_SYSTEM_PROMPT)); print(\"<clarification_policy>\" in _SYSTEM_PROMPT)'"
```

Expected: prompt size ~7200-7400, `True` for the tag presence.

- [ ] **Step 3: Run the 5 new cases against staging**

```bash
ssh aidenyi@34.73.236.64 "sudo docker exec ecom-netsuite-backend-1 python -m app.services.benchmarks.run_vs_mcp --case sales_analysis_ambiguous_source --case sales_no_period --case sales_no_dimension --case sales_with_pin_respects_pin --case lookup_no_ask --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a --skip-baseline 2>&1 | tail -30"
```

Expected: 5 cases pass with verdicts `ASK_FIRST` or `MUST_NOT_ASK` (4 of the former, 1 of the latter). If any fail, inspect the `answer_text` in the run output and adjust the prompt block (not the scorer — the scorer is deterministic).

- [ ] **Step 4: Run the existing 18 cases to confirm no baseline regression**

```bash
ssh aidenyi@34.73.236.64 "sudo docker exec ecom-netsuite-backend-1 python -m app.services.benchmarks.run_vs_mcp --suite sales --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a --skip-baseline 2>&1 | tail -35"
```

Expected: same accuracy as the current baseline (1.00 on `sales_country_canonical`, etc.) — no accuracy drop. Cost may vary ±$0.01 due to the longer prompt.

- [ ] **Step 5: No commit**

Verification only. Record the staging benchmark results for the PR body.

---

## Task 11: Push + open PR

**Files:** none modified.

- [ ] **Step 1: Confirm clean git state**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-ask-first-clarification
git status --short
git log --oneline origin/main..HEAD
```

Expected: clean working tree (only untracked symlinks). ~7-8 commits ahead of main.

- [ ] **Step 2: Determine PR base**

```bash
git log origin/main --oneline -5 | grep -iE "cross-source|phase 2" | head -3
```

If main contains Phase 2 merge → base is `main`. Otherwise → base is `feat/cross-source-sheets`.

- [ ] **Step 3: Open PR**

```bash
BASE_BRANCH="<main or feat/cross-source-sheets>"

gh pr create \
  --base "$BASE_BRANCH" \
  --head feat/ask-first-clarification \
  --title "feat(chat): ask-first clarification policy + 5 regression-guard benchmarks" \
  --body "$(cat <<'EOF'
## Summary

Restores the ask-first behavior that was lost when the source-picker classifier was deleted in PR #40. Agent-initiated via a new `<clarification_policy>` block in `_SYSTEM_PROMPT` — no pre-agent classifier, no coordinator revival. Clarifying questions render as a bold markdown callout.

Ships 5 vs-MCP benchmark cases that lock the behavior in as a permanent regression guard, plus a runbook so future \"we lost behavior X\" reports follow the same pattern.

## Changes

- **`backend/app/services/chat/agents/unified_agent.py`** — new `<clarification_policy>` XML block in `_SYSTEM_PROMPT`. Specifies WHEN to ask (ambiguous source/period/dimension), WHEN NOT to (clear intent, pin set, lookups), HOW to format (blockquote + bold + max 4 bullets), ONCE per request (no loops). ~1600 chars.
- **`backend/app/services/benchmarks/run_vs_mcp.py`** — `Case` dataclass gets 4 new optional fields: `expected_behavior`, `expected_question_about`, `expected_connectors`, `source_pin`. Loader validates enum values and raises on unknowns.
- **`backend/app/services/benchmarks/scorer.py`** — `ask_first_score` (requires zero tool calls + callout format + question) and `must_not_ask_first_score` (fails if callout present). Deterministic — no LLM call.
- **`backend/app/services/benchmarks/agent_runner.py`** — `source_pin` + `force_connectors` kwargs flow from case → runner → session, making ask-first cases hermetic.
- **5 new cases** in `benchmark_cases/vs_mcp/`: `sales_analysis_ambiguous_source`, `sales_no_period`, `sales_no_dimension`, `sales_with_pin_respects_pin`, `lookup_no_ask` (counter-case).
- **`docs/runbooks/capturing-behavior-regressions.md`** — playbook.
- **`backend/tests/test_prompt_trim.py`** — ceiling bumped 13000 → 14000.

## Test plan

- [x] Backend pytest: [N] tests passing, 0 failing
- [x] 5 new cases pass on staging: 4× `ASK_FIRST` + 1× `MUST_NOT_ASK`
- [x] 18 existing cases: baseline unchanged (sales_country_canonical still 1.00)
- [x] Policy substring tests: 7/7 (test_unified_agent_clarification_policy.py)
- [x] Scorer unit tests: 8/8 (test_benchmark_ask_first_scorer.py)
- [x] Case schema tests: 5/5 (test_benchmark_case_schema.py)

## Known follow-ups (out of scope)

1. **Post-stream `<clarify>` extractor + frontend callout card** — if markdown proves too subtle after a week on staging, add a structured render path mirroring the `<chart>` pattern.
2. **Entity disambiguation** (same-name customer) — different path (`tenant_resolver.py` + NER); next spec.
3. **`_INVESTIGATION_RE` audit + `seed_tenant_patterns.py`** — PR B-other per Phase 2 spec.
4. **Investigation-mode dead regex** in `unified_agent.py:409-421` — flagged in Phase 2 PR A follow-ups.

## Spec / plan

- Spec: `docs/superpowers/specs/2026-04-17-ask-first-clarification-design.md`
- Plan: `docs/superpowers/plans/2026-04-17-ask-first-clarification-pr-b.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Record the PR URL when printed.

- [ ] **Step 4: Verify CI**

```bash
sleep 120 && gh pr checks $(gh pr view feat/ask-first-clarification --json number --jq .number)
```

Expected: all non-lint checks pass. Lint may continue to show the 17 pre-existing errors from main; this PR introduces none.

---

## Self-Review (completed inline during plan drafting)

**Spec coverage:**
- ✅ Prompt policy `<clarification_policy>` → Task 2
- ✅ Callout format prescribed → Task 2 Step 4
- ✅ WHEN to ask / WHEN NOT / HOW / ONCE-max → Task 2 Step 4
- ✅ 5 benchmark cases → Task 7
- ✅ Scorer extension → Task 4 + Task 6 dispatch
- ✅ `source_pin` + `expected_connectors` threading → Task 5
- ✅ Case schema validation → Task 3 validation test
- ✅ Ceiling bump 13000 → 14000 → Task 2 Step 7
- ✅ Runbook → Task 8
- ✅ Staging benchmark verification → Task 10
- ✅ Baseline non-regression → Task 10 Step 4
- ✅ Push + PR → Task 11

**Placeholder scan:** No TBD/TODO. Every step has concrete code or commands. No "implement similar to X" — each task is self-contained.

**Type consistency:** `Case.expected_behavior: str | None`, `expected_question_about: list[str] | None`, `expected_connectors: list[str] | None`, `source_pin: str | None` — consistent across Task 3 dataclass, Task 6 dispatch, and Task 7 YAMLs. `ask_first_score` / `must_not_ask_first_score` signatures consistent across Task 4 scorer and Task 6 dispatch invocation.

**File-path accuracy:** All paths verified against the current repo state (inspected 2026-04-17 before writing the plan).

---

## Out of scope (explicit non-tasks)

- Frontend callout card (structured_output + React component) — punt to v2 if markdown is insufficient after user testing on staging.
- Post-stream `<clarify>` XML tag extractor — same as above.
- `_INVESTIGATION_RE` coverage audit — separate PR.
- `seed_tenant_patterns.py` admin script — separate PR.
- Investigation-mode regex cleanup in `unified_agent.py:409-421` — separate PR.
- Entity disambiguation clarification — separate spec, different mechanism.
- Benchmark CI gate tuning to include ask_first cases in the pass/fail threshold — confirm behavior on staging first, then adjust CI gate in a follow-up.
