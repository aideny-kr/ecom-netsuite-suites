# Dev/Admin Workspace: IDE-like File System + Chat File References
_Last updated: 2026-02-17_

## Objective
Make the Dev/Admin module behave like an AI IDE:
- list/search/read SuiteScript and SDF project files
- reference files in chat (e.g., `@workspace:/SuiteScripts/foo.js`)
- propose changes as diffs (change sets), never direct edits
- enforce governance, RBAC, audit logging, and (later) sandbox validate/tests/deploy

This document defines the **virtual filesystem** model, APIs/MCP tools, and chat UX requirements.

## Virtual filesystem model (Workspace FS)
Each tenant can create one or more **Workspaces**. A workspace exposes a read-only view of the current imported project state plus draft Change Sets.

Path scheme:
- `@workspace:/` = workspace root
- `@workspace:/SuiteScripts/...` = scripts
- `@workspace:/Objects/...` = SDF objects/customizations
- `@workspace:/deploy.xml` = SDF deploy scope
- `@workspace:/tests/...` = Jest unit tests (SuiteCloud Unit Testing)
- `@workspace:/docs/...` = optional in-project documentation

### Workspace snapshots
Maintain:
- `snapshot:baseline` (imported state)
- `snapshot:changeset/<id>` (draft state derived from baseline + patch)

## Required read operations
These must be safe for broader org roles (read-only posture) subject to RBAC/policy:
- **List**: file tree browsing
- **Read**: file content fetch (size limits)
- **Search**: keyword search across files (fast “IDE feel”)
- **Symbols (optional)**: function/class extraction for navigation

## Required write operations (privileged)
Writes MUST be gated:
- Developer/Admin role
- explicit Change Set approval state
- append-only audit events + correlation_id
- no direct “edit production”; sandbox-only deploy later

Write operations:
- apply unified diff patch into a Change Set
- create/update Change Set metadata (risk summary, test plan)

## APIs (tenant-scoped)
Recommended REST endpoints (or equivalent internal RPC):

Read:
- `GET /workspaces/:id/files?prefix=...`
- `GET /workspaces/:id/file?path=...`
- `GET /workspaces/:id/search?q=...&limit=...`

Write (privileged):
- `POST /workspaces/:id/changesets` (create)
- `POST /changesets/:id/patch` (apply unified diff)
- `POST /changesets/:id/approve` (approval gate)

## MCP tools (IDE-style)
Expose tools mirroring filesystem operations.

Read-only tools:
- `workspace.list_files(workspace_id, prefix)`
- `workspace.read_file(workspace_id, path)`
- `workspace.search(workspace_id, query, limit)`
- `workspace.get_symbols(workspace_id, path)` (optional)
- `workspace.explain_logic(target)` (reads only; may call search/read internally)

Change proposal tools (non-privileged):
- `workspace.propose_patch(workspace_id, instructions) -> unified_diff + risk_summary + test_plan`

Privileged tools (approval-gated):
- `workspace.apply_patch(changeset_id, unified_diff)`
- `workspace.run_validate(changeset_id)` (later)
- `workspace.run_tests(changeset_id)` (later)
- `workspace.deploy_sandbox(changeset_id)` (later)

## Chat UX requirements (IDE-grade)
- File reference autocomplete: typing `@` opens a file picker scoped to the workspace.
- “Context sidebar” listing attached files/snippets for the current chat.
- Tool steps visibility: show search/read actions and the referenced file paths.
- Answers must cite which files were used and which patches were proposed/applied.

## Governance and security
- Separate read-only tools from privileged tools.
- Pin tool manifest; reject unknown tools.
- Enforce row/size limits on file reads.
- Treat retrieved text as untrusted; do not allow it to override tool gating.
- Audit events for every tool call and patch application.

## Privileged run tools (runner-backed)
To support validate/tests/deploy, the Dev Workspace adds runner-backed tools.

Privileged tools (approval gated):
- `workspace.run_validate(changeset_id, target_env="sandbox")`
- `workspace.run_unit_tests(changeset_id)`
- `workspace.run_suiteql_assertions(changeset_id, assertions[])`
- `workspace.deploy_sandbox(changeset_id, sandbox_id)`
- `workspace.deploy_production(changeset_id)` (must be hard-disabled in beta)

Gating prerequisites:
- All privileged tools require Developer/Admin role AND Change Set state `approved_for_runs`.
- `deploy_sandbox` requires successful `run_validate` and `run_unit_tests` (and SuiteQL checks if enabled).
- All runs emit audit events and produce immutable artifacts.

See:
- `RUNNER_SERVICE.md`
- `DEV_WORKSPACE_RUNS.md`
- `SUITEQL_ASSERTIONS.md`