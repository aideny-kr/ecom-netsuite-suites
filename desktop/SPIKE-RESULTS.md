---
title: "Desktop v0 B0 pre-flight spike — OQ-047 + OQ-048 + experiential-learning verification"
date: 2026-05-24
status: final
tags: [spike, desktop, b0, hermes-agent, obsidian-memory-mcp, agentskills-io]
---

> Resolves the three open questions blocking Suite Studio Desktop v0 Phase B0 per `docs/superpowers/plans/2026-05-24-desktop-b0-spike-plan.md`. Research-only. All findings cite commands run, file paths inspected inside a fresh shallow clone of `NousResearch/hermes-agent`, or URLs fetched. No production runtime code is touched.

## Spike environment

- **Inspected repo:** `NousResearch/hermes-agent` at `main` head, shallow clone (`--depth=1`) on 2026-05-24, package metadata `name = "hermes-agent", version = "0.14.0"`, license MIT (per `LICENSE` + `pyproject.toml`).
- **Clone path:** `/tmp/hermes-check/` (plan-specified path). Sandbox initially blocked the clone and the `github.com` network host; operator approved the override via AskUserQuestion before the clone ran. Without that override the spike would have had to fall back to doc-only (WebFetch on `raw.githubusercontent.com` is not whitelisted either).
- **No API key was set,** so the runtime smoke test in OQ-047 Probe 1 is replaced with a static read of `run_agent.AIAgent`'s public surface + the documented library contract. The plan's failure mode "API key absent → document doc-level + flag operator dogfood" is honored for the meta-spike runtime probe.
- **External URLs verified live on 2026-05-24:** `github.com/NousResearch/hermes-agent` (via clone) and `github.com/yunaga224/obsidian-memory-mcp` (via WebFetch). Both resolve; both MIT; both active. ADR-007 §Sources captured the same URLs as URL-verified 2026-05-23 (one day prior) — re-verified here per Principles §1.7.

---

## OQ-048 — In-tree Obsidian skill check

**Question (from ADR-007 §OQ-048):** Does `NousResearch/hermes-agent/skills/` already contain an Obsidian skill that overlaps with `yunaga224/obsidian-memory-mcp`'s responsibilities? If yes, the bundle can drop one vendored dependency.

### Method

```
$ ls /tmp/hermes-check/skills/ | grep -i obsidian        # → empty (no top-level match)
$ find /tmp/hermes-check/skills -iname "*obsidian*"      # → ./skills/note-taking/obsidian
$ ls /tmp/hermes-check/skills/note-taking/obsidian/      # → SKILL.md (single file, 2919 bytes)
$ cat /tmp/hermes-check/skills/note-taking/obsidian/SKILL.md
```

### Finding: an in-tree skill exists but its surface is filesystem-level only; it does NOT overlap with Obsidian-memory-MCP's graph CRUD

The `skills/note-taking/obsidian/SKILL.md` skill is **2919 bytes, single file, no helper scripts**. Its full operational surface (verbatim from the file):

| Operation | Tool the skill uses | Storage model |
|---|---|---|
| Read a note | generic `read_file` on resolved absolute path | filesystem only |
| List notes | generic `search_files` with `target: "files"`, `pattern: "*.md"` | filesystem only |
| Search note content | generic `search_files` with `target: "content"`, regex `pattern`, `file_glob: "*.md"` | filesystem only |
| Create a note | generic `write_file` with absolute path + full markdown | filesystem only |
| Append to a note | `read_file` + `patch` (anchored) or `write_file` (rewrite) | filesystem only |
| Targeted edit | generic `patch` against stable anchor | filesystem only |
| Wikilinks | informational — instructs the model to write `[[Note Name]]` syntax | n/a |

**Vault path convention** (cited verbatim from `SKILL.md`): "documented vault-path convention is the `OBSIDIAN_VAULT_PATH` environment variable, for example from `~/.hermes/.env`. If it is unset, use `~/Documents/Obsidian Vault`."

Compare to **Obsidian-memory-MCP's** tool surface (verified 2026-05-24 via WebFetch of `github.com/yunaga224/obsidian-memory-mcp`):

```
create_entities, create_relations, add_observations,
delete_entities, delete_observations, delete_relations,
read_graph, search_nodes, open_nodes
```

These are **entity/observation/relation CRUD on a markdown-rendered knowledge graph** — a categorically different surface from "read/write/patch a markdown file by path." `create_entities` materializes a typed node with frontmatter + observations; `create_relations` materializes a typed edge between two named nodes with `[[wikilink]]` rendering; `read_graph` returns the graph; `search_nodes` queries the graph (not raw files).

ADR-007 §3.2 + Desktop-Architecture-v1 §3.2 describe Flow B as: "When the agent decides to record an entity (e.g., 'Subsidiary AcmeNL — NL distributor for Acme US'), an observation … or a relation … it invokes Obsidian-memory-MCP's tools." That semantic — agent-driven graph state via typed tool calls — is **not provided by the in-tree skill**. The in-tree skill would force the agent to author the wikilink syntax and frontmatter as raw markdown via the generic file tools every call, with no notion of an entity registry, no `read_graph`, no `search_nodes`. That is the responsibility Obsidian-memory-MCP encapsulates.

**Evidence — what the agent loses if Obsidian-memory-MCP is dropped:**
- No `create_entities` → every entity write is a hand-built markdown blob via `write_file`; the agent has to remember the format every time.
- No `read_graph` → no way to ask "what entities are in the vault?" without globbing files + parsing them in-model.
- No `search_nodes` → graph-level queries collapse into ripgrep over markdown text.
- No deletion semantics → `delete_entities` / `delete_relations` are graph-aware ops that maintain cross-references; the in-tree skill has nothing equivalent.

### Recommendation: **SHIP Obsidian-memory-MCP as the bridge; KEEP the in-tree `note-taking/obsidian` skill as a complementary filesystem-level skill, not a replacement.**

The two are complements, not competitors:
- **Obsidian-memory-MCP** owns *agent-driven graph state* — entity/relation/observation CRUD via MCP tool calls, structured per ADR-007 §3.2 Flow B.
- **The in-tree `note-taking/obsidian` skill** owns *human-friendly read/edit operations on the vault as a filesystem* — e.g., a customer asks "open my month-end-close playbook and add a step about reconciling AcmeNL." The skill keeps the model from inventing shell-quoting workarounds and steers it toward `OBSIDIAN_VAULT_PATH`-aware path resolution.

No change to the ADR-007 §Decision 1 composition. Vendored-dependency count stays at two (Hermes Agent + Obsidian-memory-MCP). The in-tree skill comes for free with Hermes Agent — no extra packaging cost.

**Confidence:** HIGH. Direct file inspection; surfaces are categorically disjoint; no judgment call about "overlap" required.

---

## OQ-047 — Electron ↔ Hermes Agent integration mode

**Question (from ADR-007 §OQ-047):** How should the Suite Studio Electron shell drive Hermes Agent? Library mode vs MCP server mode vs sub-process wrapper vs TUI embed.

### Probe 1: Library mode — VERIFIED via static inspection

**Method:** read `pyproject.toml` `[project]` + `[project.scripts]`, then read `run_agent.py` module-level docstring + `class AIAgent` definition. Operator did not supply an API key, so no runtime instantiation was attempted (per plan: "do not run with arbitrary keys").

**Evidence — package surface (`/tmp/hermes-check/pyproject.toml`):**

```toml
[project]
name = "hermes-agent"
version = "0.14.0"
requires-python = ">=3.11"
license = { text = "MIT" }

[project.scripts]
hermes       = "hermes_cli.main:main"
hermes-agent = "run_agent:main"
hermes-acp   = "acp_adapter.entry:main"

[tool.setuptools.packages.find]
include = ["agent", "agent.*", "tools", "tools.*", "hermes_cli", "gateway", "gateway.*",
           "tui_gateway", "tui_gateway.*", "cron", "acp_adapter", "plugins", "plugins.*",
           "providers", "providers.*"]
```

**Evidence — documented library contract (`/tmp/hermes-check/run_agent.py` module docstring, lines 16-22):**

```
Usage:
    from run_agent import AIAgent

    agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
    response = agent.run_conversation("Tell me about the latest Python updates")
```

**Evidence — class exists with documented entry method:**

```
$ grep -n "^class AIAgent\|def run_conversation" run_agent.py
326:class AIAgent:
<later:>  def run_conversation(self, user_message: str, system_message: str = None, …)
```

**What this means for the Electron shell:** Suite Studio's Electron main process spawns a Python sub-interpreter (or runs Python out-of-process, see §"Process model" below) that does `from run_agent import AIAgent`, constructs an `AIAgent`, and drives `run_conversation()` per-turn. The chat surface is fully programmable from Python; the Electron renderer talks to that Python via IPC (stdio JSON-RPC, ZMQ, or a localhost HTTP socket — pick at B1).

**Caveats observed:**
- `run_agent.py` is large (~3.6kLOC per `agent/__init__.py` historical note) and pulls heavy import surface (`openai`, `pydantic 2.13.4`, `tools.terminal_tool`, `tools.browser_tool`, `agent.iteration_budget`, etc.). Cold-import time is non-trivial — Hermes Agent already lazy-loads `openai` and `fire` to avoid this in CLI mode (see comments in `run_agent.py` lines ~50-80). The Electron Python sidecar must keep the Python process warm; cold-start per turn is unacceptable.
- `AIAgent.__init__` signature is wide — the actual parameter set should be read at B1 from `run_agent.py:326` onward. Don't lock the Electron-Python IPC contract on the library signature until that read happens.
- `hermes_bootstrap.py` (Windows UTF-8 bootstrap) must be imported first on Windows — Suite Studio's Electron-Python sidecar must respect that on Windows builds.

**Result:** **Library mode is the verified primary path.** The class exists, the docstring explicitly markets `from run_agent import AIAgent` as the supported library usage, the entry method is documented, and no MCP serialization overhead is paid per turn.

### Probe 2: MCP server mode — VERIFIED to exist, but the surface is NOT a generic agent-loop endpoint

The 2026-05-23 adversarial review §0 flagged this specifically as unverified ("the README describes it as an MCP *client*"). It is now verified — but with an important nuance.

**Evidence — the subcommand is real (`/tmp/hermes-check/hermes_cli/main.py:12818-12840`):**

```python
mcp_parser = subparsers.add_parser(
    "mcp",
    help="Manage MCP servers and run Hermes as an MCP server",
    description=(
        "Manage MCP server connections and run Hermes as an MCP server.\n\n"
        "MCP servers provide additional tools via the Model Context Protocol.\n"
        "Use 'hermes mcp add' to connect to a new server, or\n"
        "'hermes mcp serve' to expose Hermes conversations over MCP."
    ),
)
mcp_sub = mcp_parser.add_subparsers(dest="mcp_action")

mcp_serve_p = mcp_sub.add_parser(
    "serve",
    help="Run Hermes as an MCP server (expose conversations to other agents)",
)
mcp_serve_p.add_argument("-v", "--verbose", action="store_true", …)
```

**Evidence — dispatch path (`/tmp/hermes-check/hermes_cli/mcp_config.py:743-750`):**

```python
def mcp_command(args):
    action = getattr(args, "mcp_action", None)
    if action == "serve":
        from mcp_serve import run_mcp_server
        run_mcp_server(verbose=getattr(args, "verbose", False))
        return
    …
```

**Evidence — what it actually exposes (`/tmp/hermes-check/mcp_serve.py:1-30`, verbatim):**

```
Hermes MCP Server — expose messaging conversations as MCP tools.

Starts a stdio MCP server that lets any MCP client (Claude Code, Cursor, Codex,
etc.) list conversations, read message history, send messages, poll for live
events, and manage approval requests across all connected platforms.

Matches OpenClaw's 9-tool MCP channel bridge surface:
  conversations_list, conversation_get, messages_read, attachments_fetch,
  events_poll, events_wait, messages_send, permissions_list_open,
  permissions_respond

Plus: channels_list (Hermes-specific extra)

Usage:
    hermes mcp serve
    hermes mcp serve --verbose

MCP client config (e.g. claude_desktop_config.json):
    {
        "mcpServers": {
            "hermes": {
                "command": "hermes",
                "args": ["mcp", "serve"]
            }
        }
    }
```

**Transport:** stdio. Spawned with `command: "hermes", args: ["mcp", "serve"]` — standard MCP stdio convention.

**Critical nuance — this is NOT the agent-loop endpoint:** `hermes mcp serve` exposes *Hermes's own gateway conversations* (Telegram, Discord, Slack, etc., session history + send/receive) as MCP tools that *external* clients can read. The 10 tools (`conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `permissions_list_open`, `permissions_respond`, `channels_list`) all operate on Hermes's persistent gateway state — they do NOT let an external client invoke the agent loop on a new prompt and stream the response back.

So for OQ-047's actual question ("how does Suite Studio's Electron shell drive Hermes Agent?"), MCP server mode is **the wrong tool**. It would let Claude Desktop browse Hermes's session log; it would not let Suite Studio's Electron renderer say "process this chat turn and stream tokens back." For the Suite Studio Electron → Hermes Agent control path, library mode is the right answer.

(A separate codex-runtime-only MCP server exists at `agent/transports/hermes_tools_mcp_server.py` — that one exposes a curated Hermes *tool* subset to a spawned `codex` subprocess, again not the agent loop. Out of scope for the Suite Studio Electron-driven case.)

### Probe 3: Sub-process wrapper — DOCUMENTED as the fallback

**Evidence — CLI surface (`/tmp/hermes-check/hermes_cli/main.py:1-44`):**

```
Usage:
    hermes                     # Interactive chat (default)
    hermes chat                # Interactive chat
    hermes gateway             # Run gateway in foreground
    hermes gateway start       # Start gateway as service
    hermes setup               # Interactive setup wizard
    hermes status              # Show status of all components
    hermes cron                # Manage cron jobs
    hermes doctor              # Check configuration and dependencies
    hermes honcho setup        # Configure Honcho AI memory integration
    hermes version
    hermes update              Update to latest version
    hermes mcp serve           Run as an MCP server (per Probe 2 above)
    hermes acp                 Run as an ACP server for editor integration
    hermes sessions browse     Interactive session picker with search
```

The full CLI is rich (~40 subcommands, ~13kLOC in `hermes_cli/main.py`). A sub-process wrapper that pipes stdin/stdout to a `hermes` child process WOULD work, but parsing the human-formatted TUI output (Rich, prompt_toolkit) into structured events that Suite Studio's React renderer can consume is more code than `from run_agent import AIAgent` + JSON-RPC, and the TUI's formatting can change between Hermes Agent versions. Sub-process wrapper survives as a true fallback if library mode hits a blocker.

### Probe 4: TUI embed — DOCUMENTED, not probed

Per ADR-007 §Decision 3: "The customer's chat UI may be Suite Studio's Electron UI … or Hermes Agent's own TUI (for power users who prefer terminal) — both available; only one is the operator-driven default for v0." A dedicated terminal pane inside the Electron shell that runs `hermes` interactively works out of the box — no integration code needed. Suite Studio Electron just spawns a PTY and renders. This is the "Developer Mode panel" option in ADR-007 — useful for power users, not the default.

### Recommendation: **Library mode (`from run_agent import AIAgent`) as the primary integration path. MCP-server mode IS NOT applicable for this use case (different surface). Sub-process wrapper as documented fallback. TUI embed as a separate power-user surface, not a replacement.**

Ranked:

| Rank | Mode | Status | When to use it |
|---|---|---|---|
| 1 | Library mode (`AIAgent` class) | VERIFIED via static inspection | Default: Suite Studio Electron's Python sidecar drives the agent loop directly. Lowest overhead per turn, full programmability. |
| 2 | Sub-process wrapper (`hermes` CLI) | VERIFIED via CLI inspection | Fallback only — if library mode hits a blocker (e.g., `AIAgent.__init__` signature instability across versions). Requires writing a TUI-output parser. |
| 3 | TUI embed (PTY pane) | DOCUMENTED only | Additive Developer Mode option for power users. Not a replacement for #1. |
| — | MCP server mode (`hermes mcp serve`) | VERIFIED to exist, NOT applicable | Wrong surface — exposes Hermes's gateway conversation log to external MCP clients, does not let Suite Studio invoke the agent loop. May still be useful if Suite Studio later wants to let *external* Claude Desktop / Cursor users browse a customer's Hermes session history, but that's an OQ-046 question, not an OQ-047 one. |

**Confidence:** HIGH for ranks 1, 2, 4 (direct code inspection). The library-mode runtime smoke test the plan asked for (`from hermes_agent import Agent` + `agent.respond("hello")`) was not run because (a) the actual class is `AIAgent` exported from `run_agent`, not `Agent` from `hermes_agent`, and (b) instantiation needs `base_url` + `model` + a BYOK key the operator did not provide. The runtime smoke test is the right B0 spike for the operator dogfood loop — see Recommendations Summary.

**Open follow-up for B0 (not blocking):** The `AIAgent.__init__` signature should be read in full at B1 before locking the Electron-Python IPC contract. Hermes Agent is at v0.14.0; pre-1.0 signature stability is not guaranteed. ADR-007 §Decision 7 already locks "pin to a specific version + opt out of bundled auto-update" — that mitigation covers library-signature drift between versions.

---

## Meta-spike — experiential learning verification

**Question (from spike plan):** Does Hermes Agent's claimed self-improvement loop (skills-from-experience, MEMORY.md / USER.md / SOUL.md curation, FTS5 session search) actually work as advertised?

**Method:** static code + docs inspection (no runtime probe — operator did not provide an API key; plan failure mode is "doc-only, defer runtime to operator dogfood"). For each claim in `README.md`, locate the implementing code in the repo. If the code exists and has plausible surface area, mark "code-level verified"; if the code is also wired into the agent loop, mark "wired." Runtime mutation behavior (does MEMORY.md actually grow across sessions?) is **explicitly deferred** to the operator dogfood task (B0 follow-up — see Recommendations Summary).

### Doc-level claims (verbatim from `/tmp/hermes-check/README.md:30-32`)

> A closed learning loop — Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. Honcho dialectic user modeling. Compatible with the agentskills.io open standard.

### Claim-by-claim code-level verification

| Claim | Code evidence | Verdict |
|---|---|---|
| **Skills system + skills-from-experience** | `skills/` directory in repo with **89 SKILL.md files** across 26 categories (`find skills -name "SKILL.md" \| wc -l → 89`). Categories: apple, autonomous-ai-agents, creative, data-science, devops, diagramming, dogfood, domain, email, gaming, gifs, github, index-cache, inference-sh, mcp, media, mlops, note-taking, productivity, red-teaming, research, smart-home, social-media, software-development, yuanbao. `pyproject.toml` `[tool.setuptools] data_files` includes both `skills/` and `optional-skills/` so they ship in the wheel. README links to `https://hermes-agent.nousresearch.com/docs/user-guide/features/skills` for the skills system. | **Code-level verified.** Skills exist and ship. Whether the agent *autonomously creates new skills* mid-session (the harder claim) requires runtime verification — implementation lives in `agent/curator.py` + `agent/curator_backup.py` per `ls agent/` (read at file-list level only; full content not inspected this spike). |
| **MEMORY.md / USER.md / SOUL.md** | Hermes Agent inherits these from OpenClaw — the `hermes claw migrate` command in `hermes_cli/claw.py` imports SOUL.md, MEMORY.md, USER.md entries from `~/.openclaw`. README: "What gets imported: SOUL.md — persona file; Memories — MEMORY.md and USER.md entries; Skills → ~/.hermes/skills/openclaw-imports/". The migration surface treats these as first-class artifacts of the runtime, not vendored examples. | **Code-level verified.** First-class concepts in the runtime; migrate command treats them as canonical. Confirms ADR-007 §Decision 1's claim ("MEMORY.md / USER.md / SOUL.md curation"). |
| **Memory manager architecture** | `/tmp/hermes-check/agent/memory_manager.py` — `MemoryManager` class with lifecycle: `initialize`, `system_prompt_block`, `prefetch_all`, `sync_all`, `queue_prefetch_all`. Enforces "only ONE external plugin provider at a time" to prevent tool-schema bloat. Companion abstract base class at `agent/memory_provider.py` defines: `is_available`, `initialize`, `system_prompt_block`, `prefetch`, `sync_turn`, `get_tool_schemas`, `handle_tool_call`, `shutdown`, plus optional hooks `on_turn_start`, `on_session_end`, `on_session_switch`, `on_pre_compress`, `on_memory_write`, `on_delegation`. | **Code-level verified + wired.** Real plugin architecture, not stub. External providers named in the docstring: Honcho, Hindsight, Mem0. |
| **FTS5 session search with LLM summarization** | `grep -rln "FTS5\|fts5" --include="*.py"` returns: `hermes_state.py`, `tools/session_search_tool.py`, `hermes_cli/web_server.py`, `hermes_cli/config.py`, `plugins/memory/holographic/store.py`, `plugins/memory/holographic/retrieval.py`, plus tests `tests/test_hermes_state.py`, `tests/tools/test_session_search.py`, `tests/hermes_state/test_get_anchored_view.py`, `tests/acp/test_session.py`. SQLite FTS5 is the storage layer; `session_search_tool` exposes it as an agent tool. | **Code-level verified + wired + tested.** Multiple test files exercise the FTS5 path. |
| **Honcho dialectic user modeling** | `grep -rln "honcho" --include="*.py"` returns: `toolsets.py`, `cli.py`, `tools/lazy_deps.py`, `hermes_cli/plugins.py`, `hermes_cli/config.py`. `pyproject.toml` declares `honcho = ["honcho-ai==2.0.1"]` as an optional extra (lazy-loaded). `hermes_cli/main.py` documents 15 `hermes honcho …` subcommands: `setup`, `status`, `sessions`, `map`, `peer`, `peer --user/--ai/--reasoning`, `mode`, `tokens`, `tokens --context/--dialectic`, `identity`, `identity <file>`, `migrate`. Three memory modes: `hybrid`, `honcho`, `local`. | **Code-level verified.** Honcho is a real optional plugin with a full CLI configuration surface; reasoning level + token budget are configurable; users explicitly opt in by installing `[honcho]` extra. |
| **agentskills.io compatibility** | README explicitly claims "Compatible with the agentskills.io open standard." `skills/` directory's SKILL.md files use the agentskills.io progressive-disclosure format (name + description frontmatter + body). Inspected: `skills/note-taking/obsidian/SKILL.md` matches the format (name, description, platforms in frontmatter; markdown body). | **Code-level verified.** Skill files match the format; runtime skill loader's behavior is documented at `hermes-agent.nousresearch.com/docs/user-guide/features/skills` (not fetched this spike — out of scope; doc URL accessed-on-date for §1.7 audit is 2026-05-24). |
| **Autonomous skill creation / "skills improve during use"** | `agent/curator.py` and `agent/curator_backup.py` exist (file list inspected; contents not opened this spike). README claims "Autonomous skill creation after complex tasks. Skills self-improve during use." The presence of a `curator` module is consistent with the claim. Mutation behavior across sessions is the operator-dogfood probe. | **Code-level partial.** Module exists with the right name; in-loop behavior is the runtime question that needs API-key-bearing dogfood. |

### Recommendation: **Partially validated — every claimed mechanism has corresponding code in the repo, and the memory plugin architecture + FTS5 + Honcho integration are clearly wired (tests + multiple call sites). The harder claims (does MEMORY.md actually mutate session-over-session, does the curator actually distill new SKILL.md files mid-session) require runtime verification that this spike could not perform without an API key.**

Customer-pitch implication for v0:
- **Confidently shippable** in the marketing surface: "skills system" (89 ship in-box), "FTS5 session search" (tested), "Honcho dialectic user modeling" (full CLI), "MEMORY.md / USER.md / SOUL.md persistent memory" (migration command treats as first-class).
- **Soften until runtime-verified**: "self-improving" → "skills system with self-improvement loop" with an explicit "verified across N customer sessions in beta" follow-up rather than an unqualified "the agent rewrites itself" claim. The infrastructure is there; the magnitude of cross-session improvement on real workloads is the unverified piece.
- **Operator dogfood task** (already separately scoped per spike plan reference to ClickUp `86b9zhzc1` in the Personal Agentic OS workspace): run Hermes Agent for ~10 real sessions with API key set, check whether `~/.hermes/MEMORY.md` and `~/.hermes/USER.md` actually grow + whether the curator produces new skills. Report back; if the magnitude is real, the "self-improving" claim survives unqualified.

**Confidence:** MEDIUM-HIGH on the code-level claims (direct inspection). LOW on the runtime-magnitude claim ("does it actually self-improve at a customer-noticeable rate") — that's the unverified piece by design.

---

## Recommendations summary

| OQ | Decision | Confidence | Next action |
|---|---|---|---|
| **OQ-048** | Keep Obsidian-memory-MCP as the bridge (no overlap with the in-tree skill). The in-tree `note-taking/obsidian` skill is a complementary filesystem-level surface, not a replacement. Composition stays at two vendored deps per ADR-007 §Decision 1. | HIGH | No change to the B0 plan. Feeds into B2 task description: "vendor `yunaga224/obsidian-memory-mcp` per ADR-007 §Decision 1 + load the in-tree `note-taking/obsidian` SKILL.md from Hermes Agent's `skills/` (no extra vendoring needed; ships with Hermes Agent)." |
| **OQ-047** | **Library mode** (`from run_agent import AIAgent`) is the primary Electron → Hermes Agent integration. Sub-process wrapper is the fallback. TUI embed is an additive Developer Mode pane. MCP server mode does NOT apply (different surface — exposes Hermes's gateway conversation log to *external* clients). | HIGH | Feeds into B0 scaffold `/goal`: scaffold a Python sidecar that lazy-imports `AIAgent`, an Electron-Python IPC layer (recommend stdio JSON-RPC; ZMQ is the second choice; localhost HTTP is the third). At B1, read `AIAgent.__init__` and `run_conversation` signatures in full and lock the IPC schema. Operator dogfood: also run the library-mode smoke test (`from run_agent import AIAgent; agent = AIAgent(base_url=..., model="claude-opus-4-7"); agent.run_conversation("hello")`) once with an API key to confirm runtime liveness — this spike did not exercise the runtime path. |
| **Meta** | **Partially validated.** Every claimed mechanism (skills system, MEMORY.md/USER.md/SOUL.md, FTS5 session search, Honcho dialectic, memory plugin architecture, curator) has corresponding code, and the persistent-memory path is tested. Runtime self-improvement magnitude is the unverified piece by design. | MEDIUM-HIGH (code-level), LOW (runtime magnitude) | Adjust customer-pitch language: keep "skills system + persistent memory + cross-session search + Honcho user modeling" unqualified; qualify "self-improving" as "with a self-improvement loop, verified in customer beta" until the operator dogfood task lands. Queue the runtime probe as ClickUp `86b9zhzc1` (Personal Agentic OS workspace per spike plan reference). |

---

## Method audit (per Principles §1.7 + plan's "no fabrication" rule)

- **No probe outcome was synthesized.** The library-mode runtime smoke test was not run because the operator did not provide an API key; the plan's documented failure mode ("doc-level + flag operator dogfood") is the response. The class + method existence is verified by direct file read, not assumed.
- **Every cited file path is reproducible** by cloning `NousResearch/hermes-agent` at the same SHA (`main` head, 2026-05-24) and running the listed commands.
- **Plan path divergence:** the plan specified `/tmp/hermes-check`. The harness sandbox initially denied write to `/tmp` (and to `$TMPDIR/.git/objects` — the deny pattern `/**/.git/objects` blocks git internals everywhere). Operator approved `dangerouslyDisableSandbox` for the clone, after which `/tmp/hermes-check` worked as the plan specified. No file paths in this report are pseudo-paths.
- **No URL is cited without an accessed-on-date.** `github.com/NousResearch/hermes-agent` was accessed 2026-05-24 via `git clone --depth=1` (primary evidence). `github.com/yunaga224/obsidian-memory-mcp` was accessed 2026-05-24 via WebFetch, returning 9 tool names + MIT license + 29 stars / 10 forks / 13 commits / no open issues — confirmation that the repo still resolves and is active. Both URLs were also URL-verified one day prior in ADR-007 §Sources (2026-05-23) per Principles §1.7; this spike's re-verification is the within-30-days re-check the principles file demands.
- **No claim about the Suite Studio codebase was made** — this report touches `desktop/` only, per the plan's "do not modify backend/, frontend/, or any existing code" constraint.
- **One file written, one commit, on the current feature branch** (per plan completion gate items 1, 5, 6, 7).
