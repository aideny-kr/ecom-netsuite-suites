"""CI-safe tests for `desktop/runtime/sidecar.py`.

Mocks the vendored `AIAgent` class so the test runs without an API key or
the heavy Hermes Agent import surface. The live smoke test (real API call)
is documented in `desktop/README.md` and runs out-of-band.

Contract being asserted (per ADR-007 §Decision 6 + ADR-008 + /goal #3):
- `sidecar.build_agents()` returns a dict keyed `{"default", "plan"}`
- `default` model comes from `SUITE_STUDIO_MODEL_DEFAULT`, defaults to `claude-sonnet-4-6`
- `plan` model comes from `SUITE_STUDIO_MODEL_PLAN`, defaults to `claude-opus-4-7`
- `sidecar.main()` prints a non-empty response on the happy path
- `sidecar.main()` refuses to run when `ANTHROPIC_API_KEY` is missing
- /goal #3: `sidecar.build_mcp_server_config()` returns a dict with the
  `ns-suiteql` server registration (command + args + cwd + env)
- /goal #3: `sidecar.ensure_connection_template(org)` creates the template
  ~/SuiteStudio/{org}/netsuite-connection.json with placeholders if absent
- /goal #3: `sidecar.main()` calls `register_mcp_servers` BEFORE constructing
  AIAgent (so the MCP tool is available on the first run_conversation)
- /goal #3: `sidecar.main()` accepts an optional CLI prompt override
"""

import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

# Make `sidecar` importable from desktop/runtime/ without installing the package.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "runtime"))

import sidecar  # noqa: E402  (path-augmented import)


class _StubAIAgent:
    """In-test stub. Records constructor kwargs; returns a stub run_conversation result."""

    instances: list["_StubAIAgent"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _StubAIAgent.instances.append(self)

    def run_conversation(self, user_message, **kwargs):
        return {"final_response": f"stub-response to: {user_message}"}


@pytest.fixture(autouse=True)
def patch_ai_agent(monkeypatch):
    _StubAIAgent.instances.clear()
    monkeypatch.setattr(sidecar, "AIAgent", _StubAIAgent)
    yield


@pytest.fixture(autouse=True)
def patch_register_mcp_servers(monkeypatch):
    """Capture MCP server registration calls without spawning real subprocesses.

    The real `register_mcp_servers` lives in Hermes Agent's `tools.mcp_tool`
    and spawns a subprocess per server. In tests we replace it with a stub
    that records the config dict so we can assert on it.
    """
    calls: list[dict] = []

    def _stub(servers: dict) -> list[str]:
        calls.append(servers)
        return list(servers.keys())

    monkeypatch.setattr(sidecar, "register_mcp_servers", _stub)
    monkeypatch.setattr(sidecar, "_mcp_registration_calls_for_test", calls, raising=False)
    yield calls


def test_build_agents_uses_adr008_default_models(monkeypatch):
    monkeypatch.delenv("SUITE_STUDIO_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("SUITE_STUDIO_MODEL_PLAN", raising=False)

    agents = sidecar.build_agents()

    assert set(agents.keys()) == {"default", "plan"}
    assert agents["default"].kwargs["model"] == "claude-sonnet-4-6"
    assert agents["plan"].kwargs["model"] == "claude-opus-4-7"


def test_build_agents_respects_env_var_model_overrides(monkeypatch):
    # Gate #8: swapping models must be a config change, never a code change.
    monkeypatch.setenv("SUITE_STUDIO_MODEL_DEFAULT", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("SUITE_STUDIO_MODEL_PLAN", "claude-sonnet-4-6")

    agents = sidecar.build_agents()

    assert agents["default"].kwargs["model"] == "claude-haiku-4-5-20251001"
    assert agents["plan"].kwargs["model"] == "claude-sonnet-4-6"


def test_main_prints_non_empty_response_on_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))
    monkeypatch.delenv("SUITE_STUDIO_MODEL_DEFAULT", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = sidecar.main()

    assert exit_code == 0
    assert buf.getvalue().strip(), "main() must print a non-empty response to stdout"


def test_main_refuses_to_run_when_anthropic_api_key_missing(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    exit_code = sidecar.main()

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "ANTHROPIC_API_KEY" in (captured.err + captured.out)
    assert _StubAIAgent.instances == [], "No AIAgent should be constructed when the key is missing"


# ---------------------------------------------------------------------------
# /goal #3 — MCP server wiring
# ---------------------------------------------------------------------------


def test_build_mcp_server_config_declares_ns_suiteql_server(tmp_path, monkeypatch):
    """The sidecar must declare ns-suiteql with stdio command + args + env."""
    # Point at a writable tmp dir so the connection-file path resolves under it
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    config = sidecar.build_mcp_server_config()

    assert "ns-suiteql" in config, f"expected 'ns-suiteql' in MCP config, got {list(config.keys())}"
    server_cfg = config["ns-suiteql"]
    # stdio transport requires command + args
    assert "command" in server_cfg
    assert "args" in server_cfg and isinstance(server_cfg["args"], list)
    # Must spawn our server.py — args should include "server" (module) and -m
    args_str = " ".join(server_cfg["args"])
    assert "server" in args_str, f"args must invoke server module, got {server_cfg['args']}"
    # cwd must point at the ns-suiteql server directory (so `python -m server` resolves)
    assert "cwd" in server_cfg
    assert server_cfg["cwd"].endswith(os.path.join("mcp-servers", "ns-suiteql"))
    # env must carry SUITE_STUDIO_NS_CONNECTION_FILE pointing at the operator's creds file
    assert "env" in server_cfg
    conn_file = server_cfg["env"].get("SUITE_STUDIO_NS_CONNECTION_FILE")
    assert conn_file, f"env must set SUITE_STUDIO_NS_CONNECTION_FILE, got {server_cfg['env']}"
    assert conn_file.endswith("netsuite-connection.json")


def test_ensure_connection_template_creates_placeholder_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    path = sidecar.ensure_connection_template(org="default")

    assert os.path.exists(path), f"template should be created at {path}"
    data = json.loads(open(path).read())
    # Schema must match what netsuite_client.load_connection expects
    assert "account_id" in data
    assert "bearer_token" in data
    assert "expires_at" in data
    # Bearer token must be the placeholder marker — never a real token
    assert "REPLACE_ME" in data["bearer_token"], "template must use REPLACE_ME placeholder"


def test_ensure_connection_template_does_not_overwrite_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))
    # Pre-create with operator's real values (simulated)
    target = tmp_path / "SuiteStudio" / "default"
    target.mkdir(parents=True)
    existing = target / "netsuite-connection.json"
    existing.write_text(json.dumps({
        "account_id": "TSTDRV1234567",
        "bearer_token": "operator-real-token",
        "expires_at": "2026-12-31T00:00:00Z",
    }))

    sidecar.ensure_connection_template(org="default")

    data = json.loads(existing.read_text())
    assert data["bearer_token"] == "operator-real-token", \
        "ensure_connection_template must NEVER overwrite an operator-populated file"


def test_main_registers_mcp_server_before_constructing_agents(monkeypatch, tmp_path):
    """register_mcp_servers must be called BEFORE AIAgent() so the tool is available."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    # Track call order: each operation appends to this list
    order: list[str] = []

    def _stub_register(servers):
        order.append("register_mcp")
        return list(servers.keys())

    class _OrderTrackingAgent(_StubAIAgent):
        def __init__(self, **kwargs):
            order.append("AIAgent.__init__")
            super().__init__(**kwargs)

    monkeypatch.setattr(sidecar, "register_mcp_servers", _stub_register)
    monkeypatch.setattr(sidecar, "AIAgent", _OrderTrackingAgent)

    buf = io.StringIO()
    with redirect_stdout(buf):
        sidecar.main()

    assert "register_mcp" in order, "main() must call register_mcp_servers"
    assert "AIAgent.__init__" in order, "main() must construct an AIAgent"
    assert order.index("register_mcp") < order.index("AIAgent.__init__"), \
        f"register_mcp_servers must run BEFORE AIAgent.__init__; saw order: {order}"


def test_main_passes_ns_suiteql_to_register_mcp_servers(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    captured: list[dict] = []

    def _stub_register(servers):
        captured.append(servers)
        return list(servers.keys())

    monkeypatch.setattr(sidecar, "register_mcp_servers", _stub_register)

    buf = io.StringIO()
    with redirect_stdout(buf):
        sidecar.main()

    assert len(captured) == 1, f"register_mcp_servers should be called exactly once, was {len(captured)}"
    assert "ns-suiteql" in captured[0], \
        f"ns-suiteql must be registered, got {list(captured[0].keys())}"


def test_main_accepts_cli_prompt_argument(monkeypatch, tmp_path):
    """`python sidecar.py "list my NetSuite subsidiaries"` must run that prompt."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    user_prompt = "list my NetSuite subsidiaries"

    buf = io.StringIO()
    with redirect_stdout(buf):
        sidecar.main(argv=["sidecar.py", user_prompt])

    out = buf.getvalue()
    assert user_prompt in out, \
        f"expected the stub to echo the user prompt back, got: {out!r}"


def test_main_creates_connection_template_if_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))
    template_path = tmp_path / "SuiteStudio" / "default" / "netsuite-connection.json"
    assert not template_path.exists()

    buf = io.StringIO()
    with redirect_stdout(buf):
        sidecar.main()

    assert template_path.exists(), \
        f"main() must auto-create the connection-template at {template_path}"
