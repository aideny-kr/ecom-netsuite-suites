# `obsidian-memory` MCP server (Suite Studio shim)

Thin Python shim around the **vendored** `yunaga224/obsidian-memory-mcp`
Node.js MCP server at `desktop/runtime/obsidian-memory-mcp/`.

The shim itself is a small Python entry point invoked by `desktop/runtime/
sidecar.py` via Hermes Agent's MCP-client transport. It does **not**
re-implement the MCP server — it resolves the vault path from a
Suite-Studio-scoped env var, hands it to the vendored server as
`MEMORY_DIR`, and `execvpe`s `node dist/index.js`. Process replacement
keeps the stdio JSON-RPC pipes intact for Hermes Agent.

## Why a shim instead of registering the vendored server directly

The plan doc gate #3 explicitly permits a 5-line shim (see
`docs/superpowers/plans/2026-05-26-desktop-b0-obsidian-vault.md` §"Files
to create / modify"). We chose the shim because:

1. **Env-var rename.** Hermes Agent's MCP-client transport scopes env vars
   per server in the registration dict. The vendored server reads
   `MEMORY_DIR`; the rest of Suite Studio uses the
   `OBSIDIAN_VAULT_PATH` namespace. The shim is the boundary.
2. **Pre-flight gate.** The vendored server is shipped as TypeScript and
   must be compiled with `npm run build` to produce `dist/index.js`.
   The shim refuses to launch (with a structured error message pointing
   to the runbook) if `dist/index.js` is missing — a Python `KeyError`
   inside an MCP-spawned subprocess is much less useful than a one-line
   "run `npm install && npm run build`" hint at parent-process startup.
3. **Vendored code stays read-only.** The plan's non-negotiable #4
   ("DO NOT modify vendored code in `desktop/runtime/obsidian-memory-mcp/`")
   is binding — wrapping is the only path. Modifying the vendored source
   would also break `git submodule update`.

## Tool surface (post-registration)

After `register_mcp_servers({"obsidian-memory": {...}})` returns, the
AIAgent sees a tool surface prefixed by Hermes Agent's
`mcp_<server>_<tool>` sanitization. The relevant tools for `/goal #4`'s
entity-write smoke:

| Sanitized tool name                       | Vendored handler              | Purpose                                         |
| ----------------------------------------- | ----------------------------- | ----------------------------------------------- |
| `mcp_obsidian-memory_create_entities`     | `create_entities`             | Create one or more entity `.md` files          |
| `mcp_obsidian-memory_read_graph`          | `read_graph`                  | Read the full knowledge graph                  |
| `mcp_obsidian-memory_search_nodes`        | `search_nodes`                | Substring search across observations/names    |
| `mcp_obsidian-memory_open_nodes`          | `open_nodes`                  | Open specific nodes by name                    |
| `mcp_obsidian-memory_add_observations`    | `add_observations`            | Append observations to an existing entity      |
| `mcp_obsidian-memory_create_relations`    | `create_relations`            | Connect two entities with a relation           |
| `mcp_obsidian-memory_delete_entities`     | `delete_entities`             | Delete entity files                            |
| `mcp_obsidian-memory_delete_observations` | `delete_observations`         | Remove specific observations                   |
| `mcp_obsidian-memory_delete_relations`    | `delete_relations`            | Remove edges between entities                  |

## Local environment contract

| Env var                  | Required | Purpose                                                                          |
| ------------------------ | -------- | -------------------------------------------------------------------------------- |
| `OBSIDIAN_VAULT_PATH`    | yes      | Absolute path to the operator's Suite Studio vault (e.g. `~/SuiteStudio/default/`) |
| `OBSIDIAN_MEMORY_NODE`   | no       | Override node executable (defaults to `node` on `PATH`)                          |
| `OBSIDIAN_MEMORY_DIST`   | no       | Override vendored `dist/index.js` path (defaults to sibling submodule)            |

The sidecar populates these at registration time; the operator never sets
them directly.

## Build runbook

The vendored TypeScript must be compiled once after `git submodule update`:

```bash
cd desktop/runtime/obsidian-memory-mcp
npm install      # ~30s, pulls @modelcontextprotocol/sdk + gray-matter
npm run build    # ~3s, emits dist/index.js
```

`dist/` is `.gitignore`d in the vendored repo — it must be rebuilt locally
on every operator machine. CI does not currently exercise the live entity
write (gate #7 is operator-deferred — see
`desktop/SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` if present).

## See also

- `desktop/README.md` §"Vault scaffold + obsidian-memory MCP server" — operator runbook
- `desktop/runtime/obsidian-memory-mcp/README.md` — upstream documentation
- `docs/superpowers/plans/2026-05-26-desktop-b0-obsidian-vault.md` — plan doc
