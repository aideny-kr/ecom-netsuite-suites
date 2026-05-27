# Suite Studio Desktop

> Library-mode integration of Hermes Agent (vendored) into a Suite Studio Python sidecar.
> Phase B0 scaffold — see [`SPIKE-RESULTS.md`](SPIKE-RESULTS.md) for the pre-flight that resolved OQ-047 (library mode) and verified the `AIAgent` class surface.

This subtree is intentionally minimal. Subsequent `/goal`s layer Electron, Obsidian-memory-MCP, NetSuite MCP servers, packaging, and signing on top.

---

## Layout

```
desktop/
├── README.md                        # this file
├── SPIKE-RESULTS.md                 # B0 pre-flight spike (do not edit)
├── SMOKE-DEFERRAL-NS-SUITEQL.md     # /goal #3 gate-7 OR-branch closure
├── SMOKE-DEFERRAL-OBSIDIAN-VAULT.md # /goal #4 gate-7 OR-branch closure (if applicable)
├── pyproject.toml                   # desktop-specific Python deps
├── runtime/
│   ├── hermes-agent/                # vendored Hermes Agent (git submodule, pinned)
│   ├── obsidian-memory-mcp/         # vendored Obsidian Memory MCP Node server (git submodule, pinned)
│   ├── sidecar.py                   # library-mode wrapper around AIAgent + MCP wiring
│   └── mcp-servers/
│       ├── ns-suiteql/              # Suite-Studio-authored FastMCP server
│       │   ├── README.md            # server contract + config schema
│       │   ├── server.py            # FastMCP stdio entry point
│       │   └── netsuite_client.py   # SuiteQL REST call + validation
│       └── obsidian-memory/         # Python shim around vendored Node MCP server
│           ├── README.md            # shim contract + tool surface
│           └── server.py            # `node dist/index.js` exec wrapper
├── skills/
│   └── suite-studio-netsuite/       # Suite Studio NetSuite skill pack
│       ├── README.md
│       ├── SKILL.md                 # top-level skill manifest
│       └── suiteql/
│           └── SKILL.md             # dialect rules — verbatim from netsuite.yaml
└── tests/
    ├── __init__.py
    ├── test_sidecar.py              # sidecar + MCP wiring tests (mocked)
    ├── test_ns_suiteql_server.py    # ns-suiteql server + REST client tests (mocked)
    └── test_obsidian_memory_server.py # obsidian-memory shim tests (mocked)
```

Out of scope for this `/goal` (tracked in subsequent ones): `electron/`, `packaging/`, `signing/`, `update/`, `tools/self-evolution/`. Other NetSuite MCP tools (`ns_runReport`, `ns_runSavedSearch`, `ns_getRecord`, `ns_createRecord`, etc.) are also out of scope — they layer in at subsequent `/goal`s.

---

## MCP server architecture (/goal #3)

Suite Studio Desktop runs Hermes Agent in **library mode** (per ADR-007 §OQ-047 + SPIKE-RESULTS.md). To extend the agent with NetSuite capability, the sidecar spawns **Suite-Studio-authored MCP servers** as stdio subprocesses and registers them with Hermes Agent's built-in MCP-client transport.

```
┌────────────────────────────────────────────────────────────┐
│              desktop/runtime/sidecar.py                    │
│  ┌─────────────────┐   ┌─────────────────────────────────┐ │
│  │  AIAgent("default")│ │ register_mcp_servers({...})    │ │
│  │  AIAgent("plan")   │ │  ↑ from tools.mcp_tool         │ │
│  └─────────────────┘   └────────┬────────────────────────┘ │
│                                  │ stdio JSON-RPC          │
└──────────────────────────────────┼─────────────────────────┘
                                   ▼
              ┌───────────────────────────────────────┐
              │  runtime/mcp-servers/ns-suiteql/      │
              │  server.py  (FastMCP, exposes         │
              │     ns_runSuiteQL → netsuite_client)  │
              └──────────────────┬────────────────────┘
                                 │ HTTPS Bearer token
                                 ▼
              https://{account}.suitetalk.api.netsuite.com
                  /services/rest/query/v1/suiteql
```

**Registration is explicit, not config-file driven.** Hermes Agent reads MCP servers from `~/.hermes/config.yaml` by default; the sidecar instead calls `register_mcp_servers({"ns-suiteql": {...}})` directly so Suite Studio Desktop never pollutes the operator's global Hermes config. See the probed-surface notes in `runtime/sidecar.py`'s module docstring.

**The MCP server framework is the official `mcp` SDK** (MIT, Anthropic) — specifically `from mcp.server.fastmcp import FastMCP`. We chose this over the standalone `fastmcp` PyPI package (jlowin/PrefectHQ, Apache-2.0) because: (1) the official SDK provides the same `FastMCP` API surface (jlowin's project was upstreamed), (2) Hermes Agent already depends on `mcp`, so we add zero net new transitive deps, (3) MIT alignment with this project's license. See the inline license-audit comment in `pyproject.toml`.

### Populating `~/SuiteStudio/{org}/netsuite-connection.json`

The first time you run the sidecar, it auto-creates a placeholder at `~/SuiteStudio/default/netsuite-connection.json` with `REPLACE_ME` markers. **The operator populates this file out-of-band** — see [`feedback_api_key_in_goal_sessions.md`](../memory/feedback_api_key_in_goal_sessions.md): never paste real Bearer tokens into a `/goal` session, since Claude Code persists every turn to `~/.claude/projects/<repo>/<session>.jsonl` indefinitely.

```json
{
  "account_id": "TSTDRV1234567",
  "bearer_token": "eyJ...<your OAuth 2.0 access token>...",
  "expires_at": "2026-12-31T00:00:00Z"
}
```

Where to get these values:

- **`account_id`** — your NetSuite account ID, the one that appears in your SuiteTalk subdomain (e.g., `TSTDRV1234567`). Find it in NetSuite under *Setup → Company → Company Information → Account ID*.
- **`bearer_token`** — an OAuth 2.0 access token issued by NetSuite. Easiest path: use the existing Hosted backend's onboarding flow at `https://api-staging.suitestudio.ai` to complete the OAuth 2.0 PKCE handshake, then copy the access token from the `connections` table. Token refresh on 401 is **deliberately deferred** at B0 (operator re-mints when expired). Keychain integration lands at `/goal #5` (Electron + macOS keychain).
- **`expires_at`** — currently informational only; the server doesn't preemptively refresh. Set to the actual NetSuite-issued expiry so you remember when to rotate.

To switch orgs: set `SUITE_STUDIO_ORG=acme` in the env when running the sidecar — the server reads from `~/SuiteStudio/acme/netsuite-connection.json` instead.

### Smoke test commands

```bash
cd desktop
source .venv/bin/activate           # or your venv strategy
pip install -e ./runtime/hermes-agent
pip install -e '.[dev]'

# Benign smoke (no NetSuite call) — just verifies the runtime + MCP registration:
python runtime/sidecar.py

# /goal #3 gate-7 — calls NetSuite, returns subsidiary list (requires real creds):
python runtime/sidecar.py "list my NetSuite subsidiaries"
```

The CI-safe pytest suite covers both paths with mocks; the live smoke is operator-run.

### What the MCP tool surface looks like to the LLM

After `register_mcp_servers` returns, the AIAgent sees a tool named `mcp_ns_suiteql_ns_runSuiteQL(query: str) → dict` (Hermes Agent's standard `mcp_<server>_<tool>` sanitization). The tool description carries a pointer to the SuiteQL dialect rules at `desktop/skills/suite-studio-netsuite/suiteql/SKILL.md`, which is the canonical (verbatim) lift from `backend/app/services/chat/knowledge_profiles/netsuite.yaml`.

---

## Vault scaffold + `obsidian-memory` MCP server (/goal #4)

`/goal #4` wires a SECOND MCP server alongside `ns-suiteql`: a Python shim
around the vendored Node.js `yunaga224/obsidian-memory-mcp` server. The
shim translates the Suite-Studio-scoped `OBSIDIAN_VAULT_PATH` env var
into the vendored server's `MEMORY_DIR` contract, then `os.execvpe`s
`node dist/index.js` so the parent process is replaced and Hermes
Agent's stdio JSON-RPC pipes stay intact.

```
┌────────────────────────────────────────────────────────────────────┐
│                  desktop/runtime/sidecar.py                        │
│  ┌──────────────────┐   ┌──────────────────────────────────────┐  │
│  │ AIAgent          │   │ register_mcp_servers({                │  │
│  │  ("default")     │   │   "ns-suiteql":      {python, ...},   │  │
│  │ AIAgent("plan")  │   │   "obsidian-memory": {python, ...},   │  │
│  └──────────────────┘   │ })                                    │  │
│                         └────┬──────────────────────┬───────────┘  │
└──────────────────────────────┼──────────────────────┼──────────────┘
                               │ stdio                │ stdio
            ┌──────────────────▼──┐        ┌──────────▼──────────────┐
            │ mcp-servers/        │        │ mcp-servers/            │
            │   ns-suiteql/       │        │   obsidian-memory/      │
            │   server.py         │        │   server.py (shim)      │
            └─────────┬───────────┘        └────────────┬────────────┘
                      │ HTTPS                           │ os.execvpe(node, ...)
                      ▼                                 ▼
       NetSuite SuiteTalk REST          desktop/runtime/obsidian-memory-mcp/
                                                  dist/index.js
                                                      │
                                                      │ reads/writes .md
                                                      ▼
                                          ~/SuiteStudio/{org}/  ← THE VAULT
                                            ├── .obsidian/
                                            ├── 00-Home.md (frontmatter only)
                                            └── *.md  (entities, operator-authored)
```

### Vault scaffold contract

On first run, the sidecar's `ensure_vault_scaffold(org)` creates:

| Path | Content |
| --- | --- |
| `~/SuiteStudio/{org}/`               | Empty directory                                  |
| `~/SuiteStudio/{org}/.obsidian/`     | Empty — marks the folder as an Obsidian vault    |
| `~/SuiteStudio/{org}/00-Home.md`     | **Frontmatter only** — `title`, `tags: [home]`. Body strictly empty per plan non-negotiable #5 |

**The scaffold never fabricates operator content.** Vault contents
(entities, observations, relations) are tenant data, created by the
operator or written by the agent at the operator's direction via
`mcp_obsidian-memory_create_entities`. The scaffold is also idempotent:
running the sidecar twice never overwrites a 00-Home.md that the
operator has hand-edited (same idempotency contract as the ns-suiteql
connection-file template).

### Building the vendored Node.js MCP server

The vendored repo at `desktop/runtime/obsidian-memory-mcp/` ships
TypeScript source only — `dist/` is `.gitignore`d upstream and must
be rebuilt locally after every `git submodule update`:

```bash
cd desktop/runtime/obsidian-memory-mcp
npm install      # ~30s, pulls @modelcontextprotocol/sdk + gray-matter
npm run build    # ~3s, emits dist/index.js
```

If the shim is spawned before this is done, it refuses to launch with
a structured error containing the runbook above. Subsequent submodule
bumps may require re-running `npm install` + `npm run build`.

### Tool surface (post-registration)

The shim exposes 9 vendored tools, each prefixed with
`mcp_obsidian-memory_` per Hermes Agent's sanitization:

| Sanitized tool name                       | Purpose                                          |
| ----------------------------------------- | ------------------------------------------------ |
| `mcp_obsidian-memory_create_entities`     | Create one or more entity `.md` files            |
| `mcp_obsidian-memory_read_graph`          | Read the full knowledge graph                    |
| `mcp_obsidian-memory_search_nodes`        | Substring search across observations/names       |
| `mcp_obsidian-memory_open_nodes`          | Open specific nodes by name                      |
| `mcp_obsidian-memory_add_observations`    | Append observations to an existing entity        |
| `mcp_obsidian-memory_create_relations`    | Connect two entities with a relation             |
| `mcp_obsidian-memory_delete_entities`     | Delete entity files                              |
| `mcp_obsidian-memory_delete_observations` | Remove specific observations                     |
| `mcp_obsidian-memory_delete_relations`    | Remove edges between entities                    |

### Live entity-write smoke runbook (gate #7)

```bash
cd desktop
source .venv/bin/activate
pip install -e ./runtime/hermes-agent
pip install -e '.[dev]'

# 1. Build the vendored Node MCP server (one-time, see runbook above)
( cd runtime/obsidian-memory-mcp && npm install && npm run build )

# 2. Set the operator's BYOK Anthropic key (out-of-band, NEVER in a /goal session)
export ANTHROPIC_API_KEY=sk-ant-...

# 3. First run scaffolds the vault (idempotent):
python runtime/sidecar.py
# → creates ~/SuiteStudio/default/ + .obsidian/ + 00-Home.md (frontmatter only)

# 4. The gate-7 entity-write prompt:
python runtime/sidecar.py "create an entity called 'TestEntity' with observation 'first vault write 2026-05-XX'"

# 5. Verify the file landed:
ls ~/SuiteStudio/default/
# Expected: 00-Home.md, .obsidian/, TestEntity.md (the new entity file)
cat ~/SuiteStudio/default/TestEntity.md
# Expected: YAML frontmatter (entityType, created, updated) + Observations + Relations sections
```

### Capturing the live smoke result

If/when the operator runs the gate-7 smoke, paste the verbatim output
below (truncate sensitive paths if needed):

```
Date:          <YYYY-MM-DD>
Vault:         ~/SuiteStudio/default/
Model:         claude-sonnet-4-6
Prompt:        create an entity called 'TestEntity' with observation '...'
Response:      <agent's natural-language reply>
Tool surface:  mcp_obsidian-memory_create_entities — 1 invocation
File created:  ~/SuiteStudio/default/TestEntity.md
Frontmatter:   entityType: <…>, created: <…>, updated: <…>
Observations:  - first vault write 2026-05-XX
```

If gate #7 is deferred (same OR-branch precedent as /goal #2 gate #5
and /goal #3 gate #7), see `SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` in this
directory for the four-source citation.

---

## Vendoring strategy — Obsidian Memory MCP at `c3708dd`

`yunaga224/obsidian-memory-mcp` is vendored as a **git submodule** at
`desktop/runtime/obsidian-memory-mcp`, pinned at commit
**`c3708dd33d92b3b5e37d75dc7bb79be3b18606a2`** (2025-08-02).

### Why a commit SHA instead of a CalVer or SemVer tag

Unlike Hermes Agent (which uses CalVer tags), `obsidian-memory-mcp`
publishes **neither git tags nor formal releases** — verified
2026-05-26 via `git ls-remote --tags https://github.com/YuNaga224/obsidian-memory-mcp`
(returns no output). A commit SHA pin is the only stable option, and
the plan doc's OQ-049 "tag with matching package version" convention
from /goal #3 does not apply.

Commit `c3708dd` is the merge of PR #1 (a Glama badge addition by
@punkpeye) and the most recent commit on `main` as of dispatch. The
package metadata at this commit:

- `name`: `obsidian-memory-mcp`
- `version`: `1.0.0` (from `package.json`)
- `license`: **MIT** (verified at the pinned commit; LICENSE retains
  the original 2024 Anthropic copyright for the upstream memory
  server, plus 2025 YuNaga224 for the Obsidian-specific modifications)
- `type`: Node.js / TypeScript (ESM)
- `entrypoint`: `dist/index.js` (compiled via `npm run build`)
- `runtime env`: `MEMORY_DIR`

### Adding the submodule (reference)

```bash
git submodule add https://github.com/YuNaga224/obsidian-memory-mcp.git desktop/runtime/obsidian-memory-mcp
cd desktop/runtime/obsidian-memory-mcp
git checkout c3708dd33d92b3b5e37d75dc7bb79be3b18606a2
cd ../../..
git add .gitmodules desktop/runtime/obsidian-memory-mcp
git commit -m "feat(desktop): vendor obsidian-memory-mcp at c3708dd"
```

If the host sandbox blocks submodule operations on `.git/objects` or
`.gitmodules`, approve `dangerouslyDisableSandbox` for the single
`git submodule add` invocation (same pattern the Hermes Agent vendor
required at /goal #2 / /goal #3).

### Upgrading

```bash
cd desktop/runtime/obsidian-memory-mcp
git fetch
git checkout <new-sha>
cd ../../..
( cd desktop/runtime/obsidian-memory-mcp && npm install && npm run build )
cd desktop && pytest tests/test_obsidian_memory_server.py
git add desktop/runtime/obsidian-memory-mcp
git commit -m "chore(desktop): bump obsidian-memory-mcp to <new-sha>"
```

Always re-check the upstream `LICENSE` for divergence at bump time.

---

## Vendoring strategy — Hermes Agent at `v2026.5.16`

Hermes Agent is vendored as a **git submodule** at `desktop/runtime/hermes-agent`, pinned at tag **`v2026.5.16`**.

### Why a CalVer tag instead of `v0.14.0`

ADR-007 §Decision 7 and the B0 plan call for pin `v0.14.0`. **That string is the package metadata version inside `pyproject.toml`, not a git tag.** As of 2026-05-25 the Hermes Agent GitHub remote uses CalVer tags exclusively (`v2026.5.16`, `v2026.4.30`, `v2026.3.28`, …); no SemVer `v0.14.0` tag exists. The closest stable release tag prior to the goal date is **`v2026.5.16`** (released 2026-05-16), whose `pyproject.toml` declares `name = "hermes-agent"` and `version = "0.14.0"` — i.e., it is the canonical "v0.14.0" the plan refers to.

Reference: `git ls-remote --tags https://github.com/NousResearch/hermes-agent` confirms only CalVer tags. The B0 plan's documented failure mode for this exact case is "Use the closest stable tag + document the choice in README" — done here.

### Adding the submodule (reference)

```bash
git submodule add https://github.com/NousResearch/hermes-agent desktop/runtime/hermes-agent
cd desktop/runtime/hermes-agent
git checkout v2026.5.16
cd ../../..
git add .gitmodules desktop/runtime/hermes-agent
git commit -m "feat(desktop): vendor Hermes Agent at v2026.5.16 (== package v0.14.0)"
```

If the host sandbox blocks submodule operations on `.git/objects`, approve `dangerouslyDisableSandbox` for the single `git submodule add` invocation (same pattern the B0 spike used for the initial clone).

### Auto-update is disabled by omission

ADR-007 §Decision 7 requires Suite Studio to opt out of Hermes Agent's bundled auto-update. We comply by simply not vendoring or wiring any update scripts in this scaffold. Upgrading is a manual, reviewed operation (see below).

---

## Upgrading Hermes Agent

Per ADR-007 §Decision 7 cadence task. To bump:

```bash
cd desktop/runtime/hermes-agent
git fetch --tags
git checkout v2026.X.Y          # the new tag
cd ../../..
# re-run the smoke test + the sidecar test suite
cd desktop && pytest tests/
python runtime/sidecar.py        # requires ANTHROPIC_API_KEY
git add desktop/runtime/hermes-agent
git commit -m "chore(desktop): bump Hermes Agent to v2026.X.Y"
```

Always re-probe `AIAgent.__init__` signature after a bump (see `runtime/sidecar.py` docstring) — Hermes Agent is pre-1.0; signature drift is possible between minor releases.

---

## Model strategy — see [ADR-008](../../suite-studio-vault/10-Architecture/Decision-Records/ADR-008-model-strategy-desktop-v0.md)

The sidecar instantiates **two `AIAgent` objects** keyed by role:

| Role | Default model ID | Env var override |
|---|---|---|
| `default` | `claude-sonnet-4-6` | `SUITE_STUDIO_MODEL_DEFAULT` |
| `plan` | `claude-opus-4-7` | `SUITE_STUDIO_MODEL_PLAN` |

Swapping models is a **config change, never a code change**. To run the sidecar against Haiku:

```bash
SUITE_STUDIO_MODEL_DEFAULT=claude-haiku-4-5-20251001 python runtime/sidecar.py
```

The `default` agent powers the smoke test and the Electron-driven default chat surface. The `plan` agent is constructed but not exercised at smoke-test time; it gets wired into Plan Mode at B2+.

---

## Running the smoke test locally

```bash
cd desktop
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ./runtime/hermes-agent              # Hermes Agent's runtime deps (dotenv, openai, etc.)
pip install -e '.[dev]'                            # pytest
export ANTHROPIC_API_KEY=sk-ant-...                # operator's BYOK Anthropic key
python runtime/sidecar.py
```

Expected: a non-empty Claude response printed to stdout. The sidecar's smoke prompt is benign ("Hello from Suite Studio sidecar smoke test. Reply in one sentence.") and the chosen model defaults to `claude-sonnet-4-6` per ADR-008.

If the API key is missing, the sidecar exits with code `2` and prints to stderr — no arbitrary-key fallback.

### Running the CI-safe test (no API key needed)

```bash
cd desktop
pytest tests/
```

The test mocks the `AIAgent` class — no live API call, safe to run anywhere. Does NOT require Hermes Agent's runtime deps to be installed; the mock replaces the class object before any of its dependencies would be touched.

---

## First successful smoke test

**Status: PASSED on 2026-05-25.**

```
Date:           2026-05-25
Vendor pin:     desktop/runtime/hermes-agent @ v2026.5.16 (== package v0.14.0)
Provider:       anthropic (api.anthropic.com, native messages mode)
Model:          claude-sonnet-4-6 (default, from SUITE_STUDIO_MODEL_DEFAULT)
Prompt:         Hello from Suite Studio sidecar smoke test. Reply in one sentence.
Response:       Hello! Suite Studio sidecar smoke test acknowledged — all systems
                are responding normally.
API call:       #1 of 90, completed in 1.57s (cache 0/11,632 tokens — cold start)
Tool surface:   26 tools loaded (full Hermes Agent default set; none exercised
                because the smoke prompt completed in one assistant turn).
```

The `plan` AIAgent (`claude-opus-4-7`) was instantiated in the same run but not exercised — Plan Mode wiring lands at B2+. Both AIAgent objects log their model on init; both showed the expected ADR-008 defaults.

### Reproducing locally

```bash
cd desktop
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ./runtime/hermes-agent              # Hermes Agent runtime deps
pip install -e '.[dev]'                            # pytest
export ANTHROPIC_API_KEY=sk-ant-...                # your BYOK Anthropic key
python runtime/sidecar.py
```

### Notes for the next bumper

- Hermes Agent emits a banner of import warnings about missing optional tool modules (`browser-cdp`, `computer_use`, `image_gen`, `vision`, `web`, etc.). All non-fatal — the listed tools are not in the default toolset and the smoke prompt does not exercise them. Suppression is a B1+ concern (sidecar's `quiet_mode=True` kwarg).
- Hermes Agent attempts to create `~/.hermes/` for its permanent allowlist. The B0 macOS sandbox blocks that path; the warning `Failed to load permanent allowlist: '~/.hermes'` is benign for a one-shot smoke test. The Electron sidecar at B5 will need to either pre-create `~/.hermes/` or override the path via `HERMES_HOME`.

---

## /goal #3 — `ns_runSuiteQL` live smoke test

**Gate #7 status: DEFERRED to operator (OR-branch).**

The CI-safe pytest suite (38 tests on `spike/desktop-b0-mcp-suiteql`, all green as of 2026-05-25) covers the MCP server, the NetSuite REST client, and the sidecar MCP wiring with mocks. The live smoke test — running `python runtime/sidecar.py "list my NetSuite subsidiaries"` against the operator's real NetSuite — requires the operator to populate `~/SuiteStudio/default/netsuite-connection.json` out-of-band first.

This is the **same OR-branch deferral pattern as /goal #2's gate #5**, and the rule is documented in [`feedback_api_key_in_goal_sessions.md`](../memory/feedback_api_key_in_goal_sessions.md): Claude Code persists every `/goal` session turn to `~/.claude/projects/<repo>/<session>.jsonl` indefinitely; pasting a real Bearer token inline would survive there as plaintext. The `/goal` agent must defer key-touching steps to the operator.

### Operator run-through (when ready)

```bash
cd desktop
source .venv/bin/activate
pip install -e ./runtime/hermes-agent
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=sk-ant-...   # operator's BYOK Anthropic key

# 1. One-shot run to auto-create the placeholder:
python runtime/sidecar.py
# → creates ~/SuiteStudio/default/netsuite-connection.json with REPLACE_ME stubs
# → prints a "[ns-suiteql] note: ... still has placeholder values" hint

# 2. Populate the file with real NetSuite OAuth 2.0 creds (out-of-band):
#    {"account_id": "TSTDRV1234567", "bearer_token": "eyJ...", "expires_at": "..."}
#    See "Populating ~/SuiteStudio/{org}/netsuite-connection.json" above.

# 3. Run the gate-7 query:
python runtime/sidecar.py "list my NetSuite subsidiaries"
# Expected: a one-paragraph reply naming the subsidiaries in the operator's account.
```

### Capturing the result

Once the live smoke completes, paste the operator's output below (truncating sensitive subsidiary names to a count + first 1–2 entries is enough; do not commit the Bearer token or full subsidiary list if either is confidential):

```
Date:          <YYYY-MM-DD>
Account:       <account_id>
Model:         claude-sonnet-4-6
Prompt:        list my NetSuite subsidiaries
Response:      <agent's natural-language reply>
Tool surface:  ns_runSuiteQL (via mcp_ns_suiteql_ns_runSuiteQL) — 1 invocation
SuiteQL:       SELECT id, name FROM subsidiary ORDER BY name FETCH FIRST 100 ROWS ONLY
Rows:          <count> (e.g., "12 subsidiaries returned")
First entries: <Subsidiary A>, <Subsidiary B>, ...
```

Commit the capture on `spike/desktop-b0-mcp-suiteql` so the next `/goal` can see gate #7 closed.

### Failure modes (per plan doc)

If the live smoke surfaces a problem, consult the plan doc's failure-modes table at `docs/superpowers/plans/2026-05-25-desktop-b0-mcp-suiteql.md`. Most common cases:

- **401 from NetSuite** → Bearer token expired. Refresh via the Hosted backend's OAuth flow and update the file.
- **Agent doesn't call `ns_runSuiteQL`** → strengthen the `SKILL.md` description (rare; the tool name + the canonical smoke query in the SKILL.md already guide the LLM).
- **0 rows from `subsidiary`** → the account isn't OneWorld and the single root subsidiary may have a NULL `name`; try `SELECT id, country FROM subsidiary` instead.

---

## Gate-8 verification — env-var model swap is a config change, never a code change

ADR-008 mandates that swapping models must be a config change (env var), not a code change. This is verified at the **test level** by `desktop/tests/test_sidecar.py::test_build_agents_respects_env_var_model_overrides`, which sets:

```bash
SUITE_STUDIO_MODEL_DEFAULT=claude-haiku-4-5-20251001
SUITE_STUDIO_MODEL_PLAN=claude-sonnet-4-6
```

…and asserts that `build_agents()` returns AIAgent instances whose `model` kwargs match exactly those env-var values, with no code edit anywhere in `sidecar.py`. Verified locally `2026-05-25` — `pytest tests/` reports `4 passed`.

For the operator: re-run the same test with the haiku override active to confirm the contract in your environment:

```bash
SUITE_STUDIO_MODEL_DEFAULT=claude-haiku-4-5-20251001 pytest tests/test_sidecar.py::test_build_agents_respects_env_var_model_overrides -v
```

---

## Decision-point status (per `docs/superpowers/plans/2026-05-24-desktop-b0-scaffold-and-sidecar.md`)

| Question | Status | Resolved where |
|---|---|---|
| OQ-047 (Electron ↔ Hermes Agent integration mode) | RESOLVED — library mode | `SPIKE-RESULTS.md`, ADR-007 §OQ-047 |
| OQ-048 (in-tree Obsidian skill overlap) | RESOLVED — keep Obsidian-memory-MCP | `SPIKE-RESULTS.md`, ADR-007 §OQ-048 |
| OQ-049 (obsidian-memory-mcp pin target) | RESOLVED — commit SHA `c3708dd` (no tags/releases upstream) | this file §Vendoring strategy — Obsidian Memory MCP |
| Vendoring strategy for Hermes Agent | RESOLVED — git submodule at `v2026.5.16` | this file |
| Vendoring strategy for obsidian-memory-mcp | RESOLVED — git submodule at `c3708dd` | this file |
| Python env for the sidecar | RESOLVED — isolated `desktop/.venv` | `pyproject.toml` |
| Model strategy | RESOLVED — Sonnet default + Opus plan, env-var override | ADR-008 |
| Sidecar IPC contract | OPEN — locked at fifth `/goal` (Electron wiring) | next plans |
| Packaging (PyInstaller / PyOxidizer) | OPEN — operator decision after bundle-size measurement | OQ-038 |
| DB persistence (Postgres / SQLite) | OPEN — operator decision | OQ-031 |

---

## References

- [`SPIKE-RESULTS.md`](SPIKE-RESULTS.md) — B0 pre-flight spike report (2026-05-24)
- ADR-007 §Decision 6 (repo layout) + §Decision 7 (pin + opt-out auto-update) — in the Suite Studio vault
- ADR-008 — Model strategy (Sonnet default + Opus plan + env-var contract)
- Desktop-Architecture-v1.md §3 — composed runtime, library mode locked
- Plan doc: `docs/superpowers/plans/2026-05-24-desktop-b0-scaffold-and-sidecar.md`
