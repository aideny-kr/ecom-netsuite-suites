"""Suite Studio Desktop — library-mode sidecar around the vendored Hermes Agent.

This is a thin wrapper that proves the library-mode integration path (resolved by
the B0 pre-flight spike — see ``desktop/SPIKE-RESULTS.md`` and ADR-007 §OQ-047).
At ``/goal #3`` it also wires the first Suite-Studio-authored MCP server,
``ns-suiteql``, into the AIAgent via Hermes Agent's MCP-client transport. It does
NOT yet expose IPC, Obsidian-memory-MCP, or any Electron-driven surface — those
are subsequent ``/goal``s in the Desktop v0 roadmap.

Vendor pin
----------
Hermes Agent is vendored as a git submodule at ``desktop/runtime/hermes-agent``,
pinned at tag ``v2026.5.16`` (the closest stable CalVer tag whose
``pyproject.toml`` declares ``version = "0.14.0"`` — the SemVer ADR-007
§Decision 7 calls for). See ``desktop/README.md §Vendoring strategy``.

Probed AIAgent surface (run_agent.AIAgent @ v2026.5.16)
-------------------------------------------------------
The full ``__init__`` signature has many kwargs (~70). The kwargs this sidecar
sets explicitly are::

    AIAgent(
        provider: str = "anthropic",        # selects Anthropic-native auth path
        base_url: str = "https://api.anthropic.com",  # redundant given provider, but explicit
        model: str = "<from env var>",       # ADR-008: sonnet default, opus plan
    )

Other ``__init__`` kwargs left at default:
``api_key`` (resolved from ``ANTHROPIC_API_KEY`` env var via the Anthropic
provider profile in ``plugins/model-providers/anthropic/__init__.py``),
``api_mode`` (auto-resolves to ``"anthropic_messages"`` when provider ==
``"anthropic"``), ``max_iterations`` (90), ``tool_delay`` (1.0), all callback
slots (we are not yet streaming or rendering tool-call telemetry), all
provider-routing knobs (single-provider direct call), all gateway/session
metadata (single-shot smoke test, not a persistent chat surface).

``AIAgent.run_conversation(user_message, system_message=None,
conversation_history=None, task_id=None, stream_callback=None,
persist_user_message=None) -> Dict[str, Any]``

The returned dict carries ``final_response`` (the assistant text), ``messages``
(the full turn history), ``api_calls``, ``completed``, and on error paths
``failed`` + ``error``.

Probed MCP-client surface (tools.mcp_tool @ v2026.5.16)
-------------------------------------------------------
Hermes Agent's MCP-client transport is NOT an ``AIAgent`` kwarg — MCP servers
are registered into the tool registry through ``tools.mcp_tool``. Two entry
points were probed in full and only the second is used by this sidecar:

  - ``discover_mcp_tools()`` reads ``~/.hermes/config.yaml`` under the
    ``mcp_servers`` key (via ``hermes_cli.config.load_config``) and registers
    every server declared there. **Not used** — we don't want Suite Studio
    Desktop to pollute the operator's global ``~/.hermes/`` config file.

  - ``register_mcp_servers(servers: Dict[str, dict]) -> List[str]`` accepts an
    explicit dict ``{server_name: server_config}`` and registers it without
    touching the config file. **This is what we call.** Idempotent on
    re-registration; safe to invoke once per sidecar startup.

Each server config follows Hermes Agent's stdio-transport schema documented in
``tools/mcp_tool.py``::

    {
        "command": "python",
        "args": ["-m", "server"],
        "cwd": "/path/to/mcp-servers/ns-suiteql",
        "env": {
            "SUITE_STUDIO_NS_CONNECTION_FILE": "/Users/.../netsuite-connection.json",
            "PATH": "...",
        },
        "timeout": 120,           # per-tool-call timeout (default: 120)
        "connect_timeout": 60,    # initial connection timeout (default: 60)
    }

After ``register_mcp_servers`` returns, the tool surface exposed to AIAgent
includes ``mcp_ns_suiteql_ns_runSuiteQL`` (name-sanitized per Hermes Agent's
sanitization rules: ``mcp_<server>_<tool>``). The LLM sees the tool with that
prefixed name and the SuiteQL dialect rules from the skill pack at
``desktop/skills/suite-studio-netsuite/suiteql/SKILL.md``.

Environment contract (ADR-008 + /goal #3)
-----------------------------------------
``ANTHROPIC_API_KEY``        — required for live runs; the sidecar refuses to
                               run without it.
``SUITE_STUDIO_MODEL_DEFAULT`` — default agent's model, defaults to
                               ``claude-sonnet-4-6``.
``SUITE_STUDIO_MODEL_PLAN``    — plan-mode agent's model, defaults to
                               ``claude-opus-4-7``.
``SUITE_STUDIO_HOME``         — root for per-org connection files. Defaults to
                               ``~/SuiteStudio``. Override for tests / multi-host.
``SUITE_STUDIO_ORG``          — which org subdirectory under ``SUITE_STUDIO_HOME``
                               to point ns-suiteql at. Defaults to ``"default"``.

Swapping models is a config change, never a code change.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

# Hermes Agent imports — `run_agent` is the vendored AIAgent module,
# `tools.mcp_tool` is the MCP-client transport.
from run_agent import AIAgent  # vendored at desktop/runtime/hermes-agent

try:
    from tools.mcp_tool import register_mcp_servers
except ImportError:  # pragma: no cover — covered indirectly by sidecar tests
    # The `mcp` package is optional in Hermes Agent — if it's missing,
    # register_mcp_servers is a no-op stub. Surface that here so the sidecar
    # still works for non-MCP runs and tests that patch this name.
    def register_mcp_servers(servers: Dict[str, dict]) -> List[str]:  # type: ignore[no-redef]
        return []

_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_DEFAULT_MODEL_DEFAULT = "claude-sonnet-4-6"
_DEFAULT_MODEL_PLAN = "claude-opus-4-7"
_SMOKE_PROMPT = "Hello from Suite Studio sidecar smoke test. Reply in one sentence."

# Path to the bundled ns-suiteql FastMCP server. Resolved from this file's
# location at import time so the sidecar can be invoked from any cwd.
_SIDECAR_DIR = os.path.dirname(os.path.abspath(__file__))
_NS_SUITEQL_DIR = os.path.join(_SIDECAR_DIR, "mcp-servers", "ns-suiteql")

# Placeholder marker the operator replaces out-of-band. Mirrors
# `netsuite_client.PLACEHOLDER_MARKER` so the agent's structured error
# message stays consistent across the boundary.
_PLACEHOLDER = "REPLACE_ME"


def _suite_studio_home() -> str:
    """Root for per-org configuration. Honors SUITE_STUDIO_HOME for tests."""
    return os.environ.get("SUITE_STUDIO_HOME") or os.path.expanduser("~/SuiteStudio")


def _connection_file_path(org: str) -> str:
    return os.path.join(_suite_studio_home(), org, "netsuite-connection.json")


def ensure_connection_template(org: str = "default") -> str:
    """Create a placeholder netsuite-connection.json if absent. Return its path.

    The template is operator-edited out-of-band — the sidecar NEVER populates
    real credentials. The placeholder marker `REPLACE_ME` is what
    `netsuite_client.load_connection()` checks for to refuse sending it as a
    Bearer token (see plan doc §Decision points).

    Idempotent: if the file already exists, it is left untouched — never
    overwritten — so an operator-populated file survives subsequent sidecar
    runs.
    """
    path = _connection_file_path(org)
    if os.path.exists(path):
        return path

    os.makedirs(os.path.dirname(path), exist_ok=True)
    template = {
        "account_id": _PLACEHOLDER,
        "bearer_token": _PLACEHOLDER,
        "expires_at": _PLACEHOLDER,
        "_README": (
            "Populate this file with your NetSuite OAuth 2.0 Bearer token. "
            "Refresh out-of-band when the token expires. See "
            "desktop/runtime/mcp-servers/ns-suiteql/README.md for the schema."
        ),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(template, fh, indent=2)
        fh.write("\n")
    return path


def build_mcp_server_config(org: str = "default") -> Dict[str, dict]:
    """Build the MCP server config dict consumed by `register_mcp_servers`.

    One server only: ``ns-suiteql``. Stdio transport — Hermes Agent's MCP
    client spawns the subprocess and pipes JSON-RPC over stdin/stdout.

    The subprocess inherits ``PATH`` so it can find ``python``, plus the
    ``SUITE_STUDIO_NS_CONNECTION_FILE`` env var pointing at the operator's
    creds file. Everything else is deliberately scrubbed — the MCP server
    has no reason to see the operator's general environment, including
    ``ANTHROPIC_API_KEY``.
    """
    return {
        "ns-suiteql": {
            "command": sys.executable or "python",
            "args": ["-m", "server"],
            "cwd": _NS_SUITEQL_DIR,
            "env": {
                "SUITE_STUDIO_NS_CONNECTION_FILE": _connection_file_path(org),
                "PATH": os.environ.get("PATH", ""),
            },
        },
    }


def build_agents() -> Dict[str, Any]:
    """Construct the two AIAgent instances per ADR-008.

    Returns a dict keyed by role:

    - ``"default"`` — the everyday agent, model from ``SUITE_STUDIO_MODEL_DEFAULT``.
    - ``"plan"``    — the plan-mode agent, model from ``SUITE_STUDIO_MODEL_PLAN``.

    Both instances are constructed eagerly. The smoke-test path in ``main()``
    only exercises ``agents["default"]``; the ``"plan"`` instance is wired in
    at B2+ when Plan Mode lands.
    """
    return {
        "default": AIAgent(
            provider="anthropic",
            base_url=_ANTHROPIC_BASE_URL,
            model=os.environ.get("SUITE_STUDIO_MODEL_DEFAULT", _DEFAULT_MODEL_DEFAULT),
        ),
        "plan": AIAgent(
            provider="anthropic",
            base_url=_ANTHROPIC_BASE_URL,
            model=os.environ.get("SUITE_STUDIO_MODEL_PLAN", _DEFAULT_MODEL_PLAN),
        ),
    }


def _extract_response_text(result: Any) -> str:
    """Pull the assistant text from a ``run_conversation`` result.

    ``run_conversation`` returns a dict with a ``final_response`` key on the
    happy path. Defensive fallbacks: stringify the whole result if the shape
    drifts in a future Hermes Agent release (caught on the next upgrade).
    """
    if isinstance(result, dict):
        text = result.get("final_response")
        if text:
            return str(text)
    return str(result)


def main(argv: Optional[List[str]] = None) -> int:
    """Run a single conversation against Claude. Returns exit code.

    Order matters: the MCP server is registered BEFORE constructing the
    AIAgent so the tool surface includes ``mcp_ns_suiteql_ns_runSuiteQL`` on
    the first ``run_conversation`` call.

    With no CLI argument the sidecar runs the default smoke prompt (a benign
    "hello" prompt — exercises the runtime without calling NetSuite). With a
    CLI argument the sidecar runs that prompt verbatim — used for the gate-7
    live smoke test::

        python runtime/sidecar.py "list my NetSuite subsidiaries"
    """
    argv = argv if argv is not None else sys.argv

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY not set — refusing to run the smoke test.\n"
            "Set the env var to your Anthropic BYOK key and re-run.",
            file=sys.stderr,
        )
        return 2

    org = os.environ.get("SUITE_STUDIO_ORG", "default")
    template_path = ensure_connection_template(org=org)

    mcp_config = build_mcp_server_config(org=org)
    register_mcp_servers(mcp_config)

    user_prompt = argv[1] if len(argv) > 1 else _SMOKE_PROMPT

    agents = build_agents()
    result = agents["default"].run_conversation(user_prompt)
    print(_extract_response_text(result))

    # If the operator hasn't filled in real creds yet, print a follow-up hint
    # so they know where to look. Best-effort — never fail the run for this.
    try:
        with open(template_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if _PLACEHOLDER in str(payload.get("bearer_token", "")):
            print(
                f"\n[ns-suiteql] note: {template_path} still has placeholder values; "
                f"NetSuite queries will return a structured error until the operator "
                f"populates it out-of-band.",
                file=sys.stderr,
            )
    except (OSError, json.JSONDecodeError):
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
