"""CI-safe tests for `desktop/runtime/sidecar.py`.

Mocks the vendored `AIAgent` class so the test runs without an API key or
the heavy Hermes Agent import surface. The live smoke test (real API call)
is documented in `desktop/README.md` and runs out-of-band.

Contract being asserted (per ADR-007 §Decision 6 + ADR-008):
- `sidecar.build_agents()` returns a dict keyed `{"default", "plan"}`
- `default` model comes from `SUITE_STUDIO_MODEL_DEFAULT`, defaults to `claude-sonnet-4-6`
- `plan` model comes from `SUITE_STUDIO_MODEL_PLAN`, defaults to `claude-opus-4-7`
- `sidecar.main()` prints a non-empty response on the happy path
- `sidecar.main()` refuses to run when `ANTHROPIC_API_KEY` is missing
"""

import io
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


def test_main_prints_non_empty_response_on_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
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
