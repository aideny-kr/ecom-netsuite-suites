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


# ---------------------------------------------------------------------------
# /goal #4 — obsidian-memory MCP server + vault scaffold
# ---------------------------------------------------------------------------


def test_build_mcp_server_config_includes_obsidian_memory(tmp_path, monkeypatch):
    """Per gate #4: the sidecar must register BOTH ns-suiteql AND
    obsidian-memory before constructing the AIAgent."""
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    config = sidecar.build_mcp_server_config()

    assert "ns-suiteql" in config, \
        f"ns-suiteql must remain registered (gate #4 regression), got {list(config.keys())}"
    assert "obsidian-memory" in config, \
        f"obsidian-memory must be registered (gate #4), got {list(config.keys())}"


def test_obsidian_memory_config_has_correct_cwd_and_env(tmp_path, monkeypatch):
    """The obsidian-memory MCP server must spawn from the shim directory
    with OBSIDIAN_VAULT_PATH pointing at the org's vault."""
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    config = sidecar.build_mcp_server_config(org="default")

    om_cfg = config["obsidian-memory"]
    assert "command" in om_cfg
    assert "args" in om_cfg and isinstance(om_cfg["args"], list)
    # cwd must be the shim directory (mcp-servers/obsidian-memory/)
    assert "cwd" in om_cfg
    assert om_cfg["cwd"].endswith(os.path.join("mcp-servers", "obsidian-memory")), \
        f"cwd must point at the obsidian-memory shim dir, got {om_cfg['cwd']!r}"
    # env must carry OBSIDIAN_VAULT_PATH (the Suite-Studio-namespaced var
    # the shim reads — NOT the vendored MEMORY_DIR)
    vault_env = om_cfg["env"].get("OBSIDIAN_VAULT_PATH")
    assert vault_env, f"env must set OBSIDIAN_VAULT_PATH, got {om_cfg['env']}"
    # The vault path must point at ~/SuiteStudio/{org}/ (not netsuite-connection.json)
    assert vault_env.endswith(os.path.join("SuiteStudio", "default")), \
        f"OBSIDIAN_VAULT_PATH must point at the org's vault directory, got {vault_env!r}"


def test_obsidian_memory_config_respects_suite_studio_org(tmp_path, monkeypatch):
    """Switching SUITE_STUDIO_ORG must change which vault subdir is wired
    into the obsidian-memory env."""
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    config = sidecar.build_mcp_server_config(org="acme")

    vault_env = config["obsidian-memory"]["env"]["OBSIDIAN_VAULT_PATH"]
    assert vault_env.endswith(os.path.join("SuiteStudio", "acme"))


def test_ensure_vault_scaffold_creates_org_directory(tmp_path, monkeypatch):
    """The sidecar must scaffold ~/SuiteStudio/{org}/ (with .obsidian/ and
    a frontmatter-only 00-Home.md) on first run."""
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    vault = sidecar.ensure_vault_scaffold(org="default")

    assert os.path.isdir(vault), f"vault directory must be created at {vault}"
    assert vault.endswith(os.path.join("SuiteStudio", "default"))
    # .obsidian/ must exist (Obsidian's app-config dir; presence is what
    # tells Obsidian "this is a vault, not a random folder")
    assert os.path.isdir(os.path.join(vault, ".obsidian")), \
        "vault must contain .obsidian/ subdirectory"
    # 00-Home.md is the canonical landing note; must be present but
    # FRONTMATTER ONLY — no fabricated operator content
    home_path = os.path.join(vault, "00-Home.md")
    assert os.path.isfile(home_path), f"00-Home.md must be created at {home_path}"


def test_ensure_vault_scaffold_home_md_is_frontmatter_only(tmp_path, monkeypatch):
    """Per plan non-negotiable #5 + failure-modes table: vault scaffold
    must NOT fabricate operator content. 00-Home.md gets YAML frontmatter
    only; body is empty."""
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    vault = sidecar.ensure_vault_scaffold(org="default")
    body = open(os.path.join(vault, "00-Home.md")).read()

    # Must have YAML frontmatter delimiters
    assert body.startswith("---\n"), f"00-Home.md must start with YAML frontmatter, got: {body[:80]!r}"
    # Must close the frontmatter
    assert "\n---\n" in body, "00-Home.md must close its YAML frontmatter"
    # Whatever follows the closing `---\n` must contain no prose. The
    # only allowed content is whitespace. (We deliberately permit no `#`
    # heading, no body paragraph, no example links.)
    parts = body.split("\n---\n", 1)
    assert len(parts) == 2, f"frontmatter split malformed: {body!r}"
    assert parts[1].strip() == "", \
        f"00-Home.md body must be empty (no fabricated content), got: {parts[1]!r}"


def test_ensure_vault_scaffold_is_idempotent(tmp_path, monkeypatch):
    """Per plan gate #5: running the scaffold twice must NEVER overwrite
    existing files. Operator-authored 00-Home.md content must survive."""
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    # First run scaffolds the empty skeleton
    vault = sidecar.ensure_vault_scaffold(org="default")
    home_path = os.path.join(vault, "00-Home.md")

    # Operator hand-edits 00-Home.md with their real content
    operator_content = (
        "---\ntitle: home\n---\n\n"
        "# My Suite Studio Home\n\n"
        "Operator-authored content here, with [[links]] and observations.\n"
    )
    with open(home_path, "w") as fh:
        fh.write(operator_content)

    # Second run must NOT clobber the operator's content
    sidecar.ensure_vault_scaffold(org="default")

    assert open(home_path).read() == operator_content, \
        "ensure_vault_scaffold must NEVER overwrite operator-authored files"


def test_main_creates_vault_scaffold(tmp_path, monkeypatch):
    """End-to-end: `python sidecar.py` must scaffold the vault on first run."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))
    vault_path = tmp_path / "SuiteStudio" / "default"
    home_path = vault_path / "00-Home.md"
    assert not vault_path.exists()

    buf = io.StringIO()
    with redirect_stdout(buf):
        sidecar.main()

    assert vault_path.is_dir(), f"main() must scaffold {vault_path}"
    assert home_path.is_file(), f"main() must create {home_path}"


def test_main_registers_both_mcp_servers_before_agent(monkeypatch, tmp_path):
    """Per gate #4: BOTH ns-suiteql and obsidian-memory must be registered
    BEFORE the AIAgent is constructed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    captured: list[dict] = []

    def _stub_register(servers):
        captured.append(servers)
        return list(servers.keys())

    order: list[str] = []

    class _OrderTrackingAgent(_StubAIAgent):
        def __init__(self, **kwargs):
            order.append("AIAgent.__init__")
            super().__init__(**kwargs)

    def _ordered_stub_register(servers):
        order.append("register_mcp")
        captured.append(servers)
        return list(servers.keys())

    monkeypatch.setattr(sidecar, "register_mcp_servers", _ordered_stub_register)
    monkeypatch.setattr(sidecar, "AIAgent", _OrderTrackingAgent)

    buf = io.StringIO()
    with redirect_stdout(buf):
        sidecar.main()

    assert captured, "register_mcp_servers must be called"
    registered = set(captured[0].keys())
    assert {"ns-suiteql", "obsidian-memory"}.issubset(registered), \
        f"both MCP servers must be registered, got {registered}"
    assert order.index("register_mcp") < order.index("AIAgent.__init__"), \
        f"register_mcp_servers must run BEFORE AIAgent.__init__; saw order: {order}"


# ---------------------------------------------------------------------------
# /goal #5 — JSON-line stdin/stdout protocol for Electron parent process
# ---------------------------------------------------------------------------


def test_serve_json_protocol_responds_to_run_action(monkeypatch, tmp_path):
    """The serve loop must read {"action":"run","query":"..."} from stdin
    and emit {"response":"...","tokens_used":N} to stdout, one JSON per line.

    Plan gate #2: this is the contract the Electron main process speaks.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    stdin = io.StringIO(json.dumps({"action": "run", "query": "say hello"}) + "\n")
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    out = stdout.getvalue().strip().splitlines()
    assert len(out) == 1, f"expected one response line, got {out!r}"
    payload = json.loads(out[0])
    assert "response" in payload, f"expected 'response' key, got {payload!r}"
    assert "say hello" in payload["response"], \
        f"stub agent should echo the query, got {payload['response']!r}"


def test_serve_json_protocol_handles_multiple_queries_on_same_agent(monkeypatch, tmp_path):
    """One AIAgent instance must serve multiple queries — the agent is built
    once before the serve loop, then reused per query (no per-query
    construction)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    stdin_lines = (
        json.dumps({"action": "run", "query": "first"}) + "\n" +
        json.dumps({"action": "run", "query": "second"}) + "\n"
    )
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=io.StringIO(stdin_lines), stdout=stdout)

    out = stdout.getvalue().strip().splitlines()
    assert len(out) == 2, f"expected two response lines, got {out!r}"
    p1, p2 = json.loads(out[0]), json.loads(out[1])
    assert "first" in p1.get("response", ""), p1
    assert "second" in p2.get("response", ""), p2
    # Only one AIAgent instance constructed across both queries (efficient reuse)
    default_agents = [a for a in _StubAIAgent.instances if a.kwargs.get("model") == "claude-sonnet-4-6"]
    assert len(default_agents) == 1, \
        f"only one default AIAgent should be constructed, got {len(default_agents)}"


def test_serve_json_protocol_returns_error_for_malformed_json(monkeypatch, tmp_path):
    """Malformed JSON must yield {"error":"..."} on stdout, NEVER crash the
    serve loop. Subsequent valid lines must continue to be handled."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    stdin_lines = (
        "this is not json\n" +
        json.dumps({"action": "run", "query": "after error"}) + "\n"
    )
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=io.StringIO(stdin_lines), stdout=stdout)

    out = stdout.getvalue().strip().splitlines()
    assert len(out) == 2, f"expected error + recovery, got {out!r}"
    err = json.loads(out[0])
    assert "error" in err, f"first line must be an error response, got {err!r}"
    recovery = json.loads(out[1])
    assert "response" in recovery and "after error" in recovery["response"], \
        f"loop must keep serving after a malformed input, got {recovery!r}"


def test_serve_json_protocol_returns_error_for_unknown_action(monkeypatch, tmp_path):
    """Unknown action verbs must yield an error without crashing the loop."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    stdin = io.StringIO(json.dumps({"action": "telekinesis", "query": "nope"}) + "\n")
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    payload = json.loads(stdout.getvalue().strip())
    assert "error" in payload, f"unknown action must yield error, got {payload!r}"
    assert "telekinesis" in payload["error"] or "action" in payload["error"].lower(), \
        f"error must reference the bad action, got {payload!r}"


def test_serve_json_protocol_emits_error_when_agent_raises(monkeypatch, tmp_path):
    """If AIAgent.run_conversation raises, the serve loop must emit
    {"error":"..."} on stdout (so the Electron renderer can surface it),
    NOT silently swallow it or kill the process."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    class _RaisingAgent(_StubAIAgent):
        def run_conversation(self, user_message, **kwargs):
            raise RuntimeError("upstream LLM blew up")

    monkeypatch.setattr(sidecar, "AIAgent", _RaisingAgent)

    stdin = io.StringIO(json.dumps({"action": "run", "query": "trigger fail"}) + "\n")
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    payload = json.loads(stdout.getvalue().strip())
    assert "error" in payload, f"agent crash must surface as error, got {payload!r}"
    assert "upstream LLM blew up" in payload["error"]


def test_serve_json_protocol_emits_each_line_with_trailing_newline(monkeypatch, tmp_path):
    """Newline-delimited JSON: every response must end with '\\n' so the
    Electron parent's readline-based reader can frame messages correctly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    stdin = io.StringIO(json.dumps({"action": "run", "query": "framing test"}) + "\n")
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    raw = stdout.getvalue()
    assert raw.endswith("\n"), f"response must end with newline, got: {raw!r}"
    # And exactly one newline between JSON objects when there is only one
    assert raw.count("\n") == 1, f"expected one framing newline, got {raw!r}"


def test_main_serve_flag_invokes_serve_loop(monkeypatch, tmp_path):
    """`python sidecar.py --serve` must enter the JSON-line serve loop
    instead of running a one-shot conversation. This is how the Electron
    main process boots the sidecar.

    The flag must be ARGV[1] so it lives in the same slot the CLI prompt
    used to occupy — no breaking change to the prompt-arg path because
    --serve is unambiguous (no real prompt starts with two dashes).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    called = {"serve": False, "stdin": None, "stdout": None}

    def _stub_serve(stdin=None, stdout=None):
        called["serve"] = True
        called["stdin"] = stdin
        called["stdout"] = stdout

    monkeypatch.setattr(sidecar, "serve_json_protocol", _stub_serve)

    exit_code = sidecar.main(argv=["sidecar.py", "--serve"])

    assert exit_code == 0
    assert called["serve"], "main(--serve) must invoke serve_json_protocol"


def test_main_without_serve_flag_keeps_existing_cli_behaviour(monkeypatch, tmp_path):
    """Back-compat: `python sidecar.py "some prompt"` must still run a
    single conversation and exit, exactly as before. The CLI mode is
    documented in README §"Live entity-write smoke runbook" and the
    /goal #3 sidecar smoke."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("SUITE_STUDIO_HOME", str(tmp_path / "SuiteStudio"))

    serve_called = {"hit": False}

    def _stub_serve(stdin=None, stdout=None):
        serve_called["hit"] = True

    monkeypatch.setattr(sidecar, "serve_json_protocol", _stub_serve)

    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = sidecar.main(argv=["sidecar.py", "list my subsidiaries"])

    assert exit_code == 0
    assert not serve_called["hit"], \
        "main with a CLI prompt must NOT enter the serve loop"
    assert "list my subsidiaries" in buf.getvalue(), \
        "CLI mode must still echo the user prompt response"


def test_serve_json_protocol_refuses_without_anthropic_key(monkeypatch):
    """Even in serve mode, the sidecar must refuse to construct an agent
    without ANTHROPIC_API_KEY — and emit an error JSON on the first
    incoming query so Electron can surface the misconfiguration."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    stdin = io.StringIO(json.dumps({"action": "run", "query": "anything"}) + "\n")
    stdout = io.StringIO()

    sidecar.serve_json_protocol(stdin=stdin, stdout=stdout)

    payload = json.loads(stdout.getvalue().strip())
    assert "error" in payload, payload
    assert "ANTHROPIC_API_KEY" in payload["error"]
