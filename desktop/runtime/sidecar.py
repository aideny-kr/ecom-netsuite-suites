"""Suite Studio Desktop — library-mode sidecar around the vendored Hermes Agent.

This is a thin wrapper that proves the library-mode integration path (resolved by
the B0 pre-flight spike — see ``desktop/SPIKE-RESULTS.md`` and ADR-007 §OQ-047).
It does NOT yet expose IPC, NetSuite MCP wiring, Obsidian-memory-MCP, or any
Electron-driven surface — those are subsequent ``/goal``s in the Desktop v0
roadmap.

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

TODO at the fourth ``/goal`` (Electron + IPC): lock the IPC contract on top of
the kwarg set above. Many of the unused kwargs (``tool_progress_callback``,
``thinking_callback``, ``stream_delta_callback``, ``session_id``,
``gateway_session_key``, …) become first-class once the renderer needs live
event streams.

Environment contract (ADR-008)
------------------------------
``ANTHROPIC_API_KEY``        — required for live runs; the sidecar refuses to
                               run without it.
``SUITE_STUDIO_MODEL_DEFAULT`` — default agent's model, defaults to
                               ``claude-sonnet-4-6``.
``SUITE_STUDIO_MODEL_PLAN``    — plan-mode agent's model, defaults to
                               ``claude-opus-4-7``.

Swapping models is a config change, never a code change.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

from run_agent import AIAgent  # vendored at desktop/runtime/hermes-agent

_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_DEFAULT_MODEL_DEFAULT = "claude-sonnet-4-6"
_DEFAULT_MODEL_PLAN = "claude-opus-4-7"
_SMOKE_PROMPT = "Hello from Suite Studio sidecar smoke test. Reply in one sentence."


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


def main() -> int:
    """Run a single smoke-test conversation against Claude. Returns exit code."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY not set — refusing to run the smoke test.\n"
            "Set the env var to your Anthropic BYOK key and re-run.",
            file=sys.stderr,
        )
        return 2

    agents = build_agents()
    result = agents["default"].run_conversation(_SMOKE_PROMPT)
    print(_extract_response_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
