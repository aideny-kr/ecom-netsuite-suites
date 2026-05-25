# Suite Studio Desktop — B0 Scaffold + Sidecar + Library-Mode Smoke Test (second /goal)

**Date:** 2026-05-24
**Status:** ready-to-run
**Predecessor:** [`2026-05-24-desktop-b0-spike-plan.md`](2026-05-24-desktop-b0-spike-plan.md) — B0 pre-flight spike (LANDED 2026-05-24, `desktop/SPIKE-RESULTS.md`, commit `e5b60af`)
**Target branch:** `spike/desktop-b0-scaffold` (create from `main`)
**Estimated duration:** 3–6 hours wall-clock for the /goal; ~30 min operator review after
**ClickUp:** [B0 Foundation 86ba3bgy2](https://app.clickup.com/t/86ba3bgy2) (in_progress); the operator handles status transitions

---

## Scope choice — why this /goal is narrower than the operator's "scaffold + integrate + smoke test" framing

Per Principles §7.8 #3 ("Cap scope per invocation. One refactor, one feature, one audit per `/goal` run"), B0 is split into a **focused slice** here:

| /goal | Scope | Status |
|---|---|---|
| First (DONE) | B0 pre-flight spike — OQ-047 + OQ-048 + experiential-learning verification | LANDED `desktop/SPIKE-RESULTS.md` 2026-05-24 |
| **Second (THIS DOC)** | **Scaffold `desktop/` + vendor Hermes Agent v0.14.0 + Python sidecar + library-mode smoke test against Claude** | ready-to-run |
| Third | Wire Suite Studio NetSuite MCP server (`ns_runSuiteQL` only) + smoke test against operator's NetSuite via `agent.run_conversation(...)` | drafted after #2 lands |
| Fourth | Vendor Obsidian-memory-MCP + scaffold `~/SuiteStudio/{org}/` vault + first entity write via `create_entities` | after #3 |
| Fifth | Electron shell + IPC to the sidecar; UI renders the conversation | after #4 |
| Sixth | First skill-pack workflow (Propose ADR) + first HITL audit_events flow + vault render | after #5 |

The temptation is to do all of B0 in one /goal. Don't. Each slice above has a single, gradable, ship-or-don't-ship test gate. A multi-objective /goal will burn cycles oscillating between sub-decisions (PyInstaller vs PyOxidizer, Postgres vs SQLite, vendoring strategy, IPC choice) instead of converging on the test gate.

**This /goal's slice is the foundation everything else builds on:** "library-mode AIAgent works inside our repo." If this lands cleanly, the next four /goals each layer one capability on top with their own gates.

---

## Goal

Produce a working **library-mode smoke test** that proves Suite Studio's Python sidecar can drive Hermes Agent's `AIAgent` class to get a response from Claude. Side effect: `desktop/` is scaffolded with the layout from ADR-007 §Decision 6, Hermes Agent v0.14.0 is vendored at a stable pin, and `desktop/README.md` documents the vendoring + upgrade procedure.

**What this /goal does NOT do (deliberate; tracked in next-/goal plans):**
- Electron shell, IPC, UI — fourth /goal
- Obsidian-memory-MCP, vault rendering — fourth /goal
- Suite Studio NetSuite MCP servers (`ns_runSuiteQL`, etc.) — third /goal
- HITL flows, audit_events rendering — sixth /goal
- OS keychain integration — fifth or later (depends on Electron)
- Packaging (PyInstaller / PyOxidizer) — deferred; requires operator decision on OQ-038
- DB persistence (Postgres / SQLite) — deferred; requires operator decision on OQ-031 after bundle-size measurement
- Auto-update — deferred to B3

---

## Why this matters (context)

The B0 pre-flight spike (LANDED 2026-05-24) resolved:
- **OQ-047:** Library mode (`from run_agent import AIAgent`) is the integration path. The class is `AIAgent` in `run_agent.py` (NOT `Agent` in `hermes_agent` — that was an ADR-007 placeholder, corrected 2026-05-24). Documented usage: `agent = AIAgent(base_url=..., model=...); agent.run_conversation(user_message, ...)`.
- **OQ-048:** Keep Obsidian-memory-MCP. The in-tree `note-taking/obsidian` skill is filesystem-level only (read/write/patch); Obsidian-memory-MCP provides graph-level CRUD (`create_entities`, `create_relations`, etc.). Complementary.
- **Meta-spike:** All claimed mechanisms have code (89 in-tree skills, FTS5 with tests, Honcho, MemoryManager plugin architecture, curator). Runtime self-improvement magnitude still operator-dogfood.

What the spike did NOT do (deferred to this /goal per the spike plan's constraint "do NOT scaffold desktop/ further"):
- Actually run `AIAgent` with an API key — the spike verified the API surface statically only.
- Vendor Hermes Agent into the repo.
- Scaffold `desktop/`.

This /goal closes those three.

---

## Inputs (what the dispatched agent reads)

**Codebase (read-only — current repo, auto-loaded via CLAUDE.md):**
1. `CLAUDE.md` — Development Workflow + URL-verify rule (Principles §1.7 mirror)
2. `desktop/SPIKE-RESULTS.md` — the spike report (verified surface + AIAgent contract)
3. `docs/superpowers/plans/2026-05-24-desktop-b0-spike-plan.md` — the prior /goal plan (for context on what's already decided)

**Vault (read-only — separate repo at `/Users/aidenyi/projects/suite-studio-vault/`):**
1. `10-Architecture/Decision-Records/ADR-007-agentic-os-foundation.md` — locked composition + §Decision 6 repo layout + §OQ-047 RESOLVED block
2. `30-Specs/Desktop-Architecture-v1.md` §3 — composed-runtime design (updated 2026-05-24 with library mode locked)
3. `10-Architecture/Principles.md` §1.7, §7.1 (TDD), §7.8 (`/goal`), §7.11 (dev cycle)
4. `10-Architecture/Reusable-Packages.md` — module organization (Desktop-specific section)

**External (URL-verify per §1.7; do not assume from prior conversation):**
- `https://github.com/NousResearch/hermes-agent` at tag/SHA `v0.14.0` — verified MIT, accessed 2026-05-24
- Operator's `ANTHROPIC_API_KEY` (required for the smoke test; agent stops + reports if missing)

---

## Pre-flight checklist (do BEFORE invoking /goal)

```
[ ] git checkout main && git pull
[ ] git checkout -b spike/desktop-b0-scaffold
[ ] CONFIRM ANTHROPIC_API_KEY is set in your shell:
    echo $ANTHROPIC_API_KEY | head -c 10   # should print the first chars; if empty, set it first
    This /goal NEEDS the key — it runs a live smoke test, not a static check.
[ ] CONFIRM operator preference on vendoring strategy (see "Decision points" below). If unsure, accept the default (git submodule); the /goal will lock it.
[ ] Open a NEW Claude Code session in this repo
[ ] Enter Plan Mode (Shift+Tab) BEFORE pasting the prompt
[ ] Sandbox: this /goal does NOT need to clone any new repos (Hermes Agent is added as a submodule, which is a local git operation). If the harness denies the submodule add, retry with /sandbox approval — same pattern as the first /goal.
```

---

## Decision points the operator should pre-decide (so /goal doesn't get stuck)

| Decision | Default in this plan | Override how |
|---|---|---|
| **Vendoring strategy for Hermes Agent** | `git submodule add https://github.com/NousResearch/hermes-agent desktop/runtime/hermes-agent` pinned at tag `v0.14.0`. Visible in repo; standard upgrade via `git submodule update`. | If you prefer pip-install-from-pin (`hermes-agent==0.14.0` in `desktop/pyproject.toml`), say so in your /goal invocation. Avoid vendored copy (`cp -r`) — bloats repo. |
| **Python env for the sidecar** | New `desktop/.venv` (Python 3.11+, isolated from `backend/.venv`). `desktop/pyproject.toml` declares `hermes-agent` (via submodule path or pin) + minimal sidecar deps. | If you prefer reusing `backend/.venv`, say so; the /goal will install Hermes Agent into it instead. Trade-off: faster onboarding vs dependency entanglement with the FastAPI backend. |
| **Model for the smoke test** | `claude-opus-4-7` (per your latest dogfood preference; matches Hermes Agent docstring example shape) | Override if you want a cheaper model for the smoke test (`claude-haiku-4-5-20251001`). |
| **Sidecar entry-point shape** | Single-file `desktop/runtime/sidecar.py` with a `main()` function that instantiates `AIAgent` and runs one `run_conversation()` call. Designed to grow into an IPC server in the fourth /goal. | Larger module structure now? Defer — keep small for this /goal. |
| **Test framework for `desktop/tests/`** | `pytest` (matches `backend/`). One test that mocks `AIAgent` and asserts the sidecar wires correctly without a live API call (CI-safe). | If you want a different framework, say so. |

---

## The /goal prompt (copy-paste verbatim)

> Paste between the fences into Claude Code, in Plan Mode, on the `spike/desktop-b0-scaffold` branch.

````
/goal Scaffold the `desktop/` subdirectory for Suite Studio Desktop v0 per ADR-007 §Decision 6, vendor Hermes Agent v0.14.0 as a git submodule, write a thin Python sidecar that drives Hermes Agent in library mode, and prove it works with a live smoke test that gets a response from Claude. ONE focused slice — do NOT add Electron, do NOT wire Obsidian-memory-MCP, do NOT add NetSuite MCP servers, do NOT touch backend/ or frontend/. Those are the next four /goals.

CONTEXT (read in order):
1. desktop/SPIKE-RESULTS.md (in this repo) — the B0 pre-flight spike that resolved OQ-047 (library mode) and verified the AIAgent class surface.
2. /Users/aidenyi/projects/suite-studio-vault/10-Architecture/Decision-Records/ADR-007-agentic-os-foundation.md — locked Agentic OS composition + §Decision 6 repo layout + §OQ-047 RESOLVED block.
3. /Users/aidenyi/projects/suite-studio-vault/30-Specs/Desktop-Architecture-v1.md §3 — composed-runtime design with library mode locked.
4. /Users/aidenyi/projects/suite-studio-vault/10-Architecture/Principles.md §1.7 (URL-verify), §7.1 (TDD), §7.8 (/goal), §7.11 (dev cycle).
5. CLAUDE.md in this repo (auto-loaded) — Development Workflow + URL-verify rule.

GOAL: Library-mode AIAgent runs from our repo and gets a response from Claude.

SCAFFOLD (per ADR-007 §Decision 6):
desktop/
├── README.md                        # NEW — document vendoring strategy + upgrade procedure
├── pyproject.toml                   # NEW — desktop-specific Python deps (hermes-agent via submodule path, pytest, ...)
├── runtime/
│   ├── hermes-agent/                # NEW — git submodule pinned at v0.14.0
│   └── sidecar.py                   # NEW — library-mode wrapper around AIAgent
├── skills/
│   └── suite-studio-netsuite/
│       ├── README.md                # NEW — placeholder; productization plan summary
│       └── SKILL.md                 # NEW — minimal valid agentskills.io SKILL.md stub (name + description + body)
├── tests/
│   ├── __init__.py                  # NEW
│   └── test_sidecar.py              # NEW — pytest, mocks AIAgent, CI-safe
└── SPIKE-RESULTS.md                 # EXISTS — leave untouched

Do NOT create in this /goal: electron/, packaging/, signing/, update/, runtime/obsidian-memory-mcp/, tools/self-evolution/. Those are subsequent /goals.

VENDORING — default is git submodule at v0.14.0:
git submodule add https://github.com/NousResearch/hermes-agent desktop/runtime/hermes-agent
cd desktop/runtime/hermes-agent && git checkout v0.14.0 && cd ../../..
git add .gitmodules desktop/runtime/hermes-agent

If submodule fails (sandbox, network, or operator-overrode to pip-install), document the chosen alternative in desktop/README.md and proceed. Either way: pin to v0.14.0, disable bundled auto-update by NOT including any auto-update scripts in this scaffold.

SIDECAR (desktop/runtime/sidecar.py):
Thin wrapper. Loads AIAgent from the vendored path. Single main() function. Reads ANTHROPIC_API_KEY from env. Calls agent.run_conversation() with a single prompt. Prints the response. Designed to grow into an IPC server in the fourth /goal — DO NOT add IPC scaffolding yet.

PROBE the AIAgent.__init__ signature in full (per ADR-007 §OQ-047 caveat) before locking sidecar kwargs:
$ grep -n "^class AIAgent\|def __init__\|def run_conversation" desktop/runtime/hermes-agent/run_agent.py | head -20
Read the signature; document any required kwargs beyond base_url + model in the sidecar's module docstring.

TDD per Principles §7.1:
Write desktop/tests/test_sidecar.py FIRST. It must:
- Mock the AIAgent class (no live API call in CI)
- Assert sidecar.main() instantiates AIAgent with the expected kwargs
- Assert sidecar.main() prints something non-empty to stdout when AIAgent.run_conversation returns a stub string
- Pass with pytest before any sidecar implementation lands

Then implement sidecar.py to make the test pass.

LIVE SMOKE TEST (the actual completion gate, runs once, not in CI):
With ANTHROPIC_API_KEY set in the environment, run:
$ cd desktop && python runtime/sidecar.py
Expected: a non-empty response from Claude printed to stdout. If the API key is missing, STOP and report — do not run with arbitrary keys.

Capture the smoke-test output in desktop/README.md under a "First successful smoke test" section with the date + the prompt sent + a 1-2 sentence excerpt of the response (do NOT paste sensitive content — the prompt should be benign like "Hello from Suite Studio sidecar smoke test").

DOCUMENT in desktop/README.md:
- Vendoring strategy chosen (submodule v0.14.0 / pip pin / other)
- How to upgrade Hermes Agent (the cadence task from ADR-007 §Decision 7)
- How to run the smoke test locally
- Decision-point status (which OQs are resolved; which still need operator input for subsequent /goals)
- Reference to ADR-007 + Desktop-Architecture-v1 §3

COMMIT POLICY:
- One commit per logical step. Order: (1) scaffold dirs + READMEs + pyproject.toml, (2) submodule add Hermes Agent, (3) failing test, (4) sidecar implementation + passing test, (5) live smoke test output in README. NEVER amend.
- Stay on spike/desktop-b0-scaffold branch.
- Do NOT push to main.

CONSTRAINTS:
- URL-verify everything (Principles §1.7 + CLAUDE.md mirror). Re-verify the Hermes Agent repo URL + v0.14.0 tag existence before adding the submodule.
- Do NOT modify backend/, frontend/, or any other existing code.
- Do NOT scaffold files not listed under SCAFFOLD above.
- Do NOT invent module names. The class is `AIAgent` in `run_agent.py`, NOT `Agent` in `hermes_agent` (per spike report).
- If ANTHROPIC_API_KEY is missing: stop, write desktop/README.md noting smoke test deferred to operator, commit, mark the live-gate item incomplete in the completion criteria. Operator runs the smoke test manually.
- Max 15 iterations per task per CLAUDE.md.

COMPLETION GATE (the supervisor judges):
1. desktop/ contains the SCAFFOLD listed above — no more, no less
2. Hermes Agent vendored at v0.14.0 (submodule, pip pin, or documented alternative)
3. desktop/runtime/sidecar.py exists and has a module docstring documenting the AIAgent kwargs probed
4. desktop/tests/test_sidecar.py passes (CI-safe; mocks AIAgent)
5. ONE OF: (a) live smoke test succeeded — captured output in desktop/README.md, OR (b) ANTHROPIC_API_KEY missing — README documents deferral to operator
6. desktop/README.md exists with vendoring strategy + upgrade procedure + smoke test instructions
7. desktop/skills/suite-studio-netsuite/SKILL.md is a valid agentskills.io SKILL.md stub (name + description frontmatter + body)
8. Commits on spike/desktop-b0-scaffold branch only; not on main
9. desktop/SPIKE-RESULTS.md untouched
10. backend/, frontend/, CLAUDE.md, vault — all untouched
````

---

## Plan Mode review checklist (before approving)

When Plan Mode produces its plan, verify the agent intends to:

```
[ ] Read the 5 CONTEXT files listed (especially SPIKE-RESULTS.md and ADR-007 §OQ-047)
[ ] Use `AIAgent` from `run_agent` (NOT `Agent` from `hermes_agent`) — the spike report names this explicitly
[ ] Vendor Hermes Agent at v0.14.0 specifically (not main, not latest)
[ ] Write the test FIRST (TDD per §7.1), implement second
[ ] Check ANTHROPIC_API_KEY is set BEFORE attempting the live smoke test
[ ] Capture the smoke test output verbatim in desktop/README.md (don't summarize)
[ ] Stay on the spike/desktop-b0-scaffold branch
[ ] NOT modify backend/, frontend/, CLAUDE.md, or the vault
[ ] NOT scaffold electron/, packaging/, runtime/obsidian-memory-mcp/, etc. — those are next /goals
```

**Redirect triggers — do NOT approve if the plan proposes:**
- Modifying `backend/app/services/chat/` or `backend/app/mcp/` — out of scope
- Vendoring Hermes Agent at `main` instead of a pinned tag — violates ADR-007 §Decision 7
- Skipping the failing-test step (TDD violation)
- Running the live smoke test with a hardcoded API key (security)
- Adding Electron / IPC / Obsidian-memory-MCP — those are subsequent /goals
- Adding a packaging step (PyInstaller / PyOxidizer) — deferred decision

---

## Expected output

After /goal completes, `desktop/` should look like:

```
desktop/
├── README.md                        # vendoring + upgrade + smoke test docs
├── SPIKE-RESULTS.md                 # unchanged from first /goal
├── pyproject.toml                   # desktop python deps
├── runtime/
│   ├── hermes-agent/                # submodule at v0.14.0
│   │   └── (full Hermes Agent repo tree)
│   └── sidecar.py                   # library-mode wrapper
├── skills/
│   └── suite-studio-netsuite/
│       ├── README.md                # productization plan summary
│       └── SKILL.md                 # agentskills.io stub
└── tests/
    ├── __init__.py
    └── test_sidecar.py              # CI-safe mocked test
```

Plus 4–5 commits on `spike/desktop-b0-scaffold` and an entry in `desktop/README.md` capturing the live smoke test output (date + benign prompt + response excerpt).

---

## Completion criteria

The supervisor inside /goal judges these 10 gates (verbatim from the prompt):

1. Scaffold matches exactly
2. Hermes Agent vendored at v0.14.0
3. Sidecar exists with docstring documenting `AIAgent.__init__` kwargs
4. Test passes (CI-safe, mocked)
5. Live smoke test succeeded OR documented deferral
6. README exists with vendoring + upgrade + smoke test docs
7. Skill pack stub is valid SKILL.md
8. Commits on spike branch only
9. SPIKE-RESULTS.md untouched
10. backend/, frontend/, CLAUDE.md, vault untouched

If gate 5 is "deferred to operator," the /goal can still mark complete — the operator runs `python desktop/runtime/sidecar.py` manually and captures the output.

---

## Post-completion checklist

```
[ ] git pull the branch + cd to desktop/
[ ] python -m venv .venv && source .venv/bin/activate && pip install -e .
    (or whichever venv strategy the /goal chose)
[ ] python runtime/sidecar.py  →  expect a non-empty Claude response
[ ] pytest tests/  →  expect green
[ ] Read desktop/README.md end-to-end — does the vendoring strategy match your preference?
[ ] If sidecar.py docstring documents extra AIAgent kwargs you weren't expecting, decide if any belong in the locked IPC contract (fourth /goal lock)
[ ] Update ClickUp B0 (86ba3bgy2): note "scaffold + sidecar landed" in description; move to qa or keep in_progress depending on whether you want to verify on a staging box
[ ] Open a PR for visibility (no need to merge — feeds into the third /goal)
[ ] Vault session log: add 2026-05-XX entry for "B0 scaffold + sidecar landed via second /goal"
[ ] Decide: kick off the third /goal (wire ns_runSuiteQL) right away, or pause for the operator-dogfood meta-spike on Hermes Agent self-improvement (ClickUp 86b9zhzc1)?
```

---

## Failure modes + fallbacks

| If… | Then… |
|---|---|
| `git submodule add` fails (sandbox denies submodule init) | Approve `dangerouslyDisableSandbox` for the single git command; same pattern as the first /goal. |
| Hermes Agent v0.14.0 tag doesn't exist on GitHub (renamed, retracted) | Stop. Verify the tag at https://github.com/NousResearch/hermes-agent/tags. Use the closest stable tag + document the choice in README. |
| `pip install -e desktop/runtime/hermes-agent` fails (missing system deps) | Read Hermes Agent's `pyproject.toml` for system requirements. Document any missing system deps + their install command in `desktop/README.md`. Do NOT silently skip — the smoke test will fail with a less clear error otherwise. |
| `AIAgent.__init__` requires kwargs the sidecar doesn't pass (e.g., a tools config) | Read the signature in full, pass the minimum required, document the rest in the sidecar docstring with "TODO: lock at fourth /goal (IPC contract)." |
| Live smoke test returns an API error (rate limit, invalid key, unsupported model) | Document the error in README + smoke test section. Do NOT silently retry with arbitrary fallbacks. Operator resolves + reruns manually. |
| `agent.run_conversation()` hangs (Hermes Agent waiting for input or stuck in a loop) | After 60 seconds, stop. Document in README. Likely root cause: Hermes Agent expects an interactive surface; check for a non-interactive flag in `AIAgent.__init__` (`headless=True`, `interactive=False`, or similar). |
| The /goal's test mock doesn't match the real AIAgent signature | Probe the signature in the spike's existing /tmp/hermes-check (if still present) or from the vendored submodule. The test is CI-safe — the mock is allowed to be approximate; the LIVE smoke test is the real signature check. |

---

## After this lands: the third /goal preview

> **Goal**: Add Suite Studio NetSuite MCP server (`ns_runSuiteQL` only, ported from `backend/app/mcp/`) to `desktop/`, wire it into the sidecar's AIAgent instantiation, and prove the agent can answer one NetSuite question (e.g., "list my subsidiaries") via `agent.run_conversation()` against the operator's NetSuite.

That /goal's completion gate: `python desktop/runtime/sidecar.py "list my NetSuite subsidiaries"` returns a real list from the operator's NetSuite. Single additive capability, single new test gate.

---

## Glossary — external proper nouns (URL-verified 2026-05-23/24 per Principles §1.7)

| Name | Repo / URL | Pin for v0 | License | Role |
|---|---|---|---|---|
| Hermes Agent | github.com/NousResearch/hermes-agent | **v0.14.0** | MIT | Agent runtime (library mode: `from run_agent import AIAgent`) |
| Obsidian-memory-MCP | github.com/yunaga224/obsidian-memory-mcp | pin at fourth /goal | MIT | Agent ↔ vault graph bridge (fourth /goal) |
| agentskills.io | agentskills.io | spec current | Open standard | SKILL.md format the skill pack targets |

All entries verified via direct clone + WebFetch on 2026-05-24. Re-verify before relying on any claim older than ~30 days.

---

## Status of this plan doc

- **Plan doc:** ready-to-run
- **Predecessor (/goal #1):** LANDED 2026-05-24, `desktop/SPIKE-RESULTS.md`, commit `e5b60af`
- **Vault context updated:** 2026-05-24 vault commit `04bbaec` (ADR-007 §OQ-047/048 resolutions + Desktop-Architecture-v1 §3.1 library-mode lock + adversarial review §0 gap-closure addendum)
- **ClickUp B0:** [86ba3bgy2](https://app.clickup.com/t/86ba3bgy2) — description updated 2026-05-24 to reference resolved OQs + library mode lock
- **ClickUp B2:** [86ba3bh4b](https://app.clickup.com/t/86ba3bh4b) — description updated 2026-05-24 with library-mode integration specifics
