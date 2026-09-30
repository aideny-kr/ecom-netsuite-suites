# backend/tests/test_thinking_config.py
from app.core.config import Settings


def test_thinking_defaults():
    s = Settings()
    assert s.CHAT_THINKING_ENABLED is True
    # The user's rule (2026-09-29): never "low"; "med" is the lowest level. Sonnet 5.5 at med
    # was its fastest and most accurate level on the vs-MCP sales suite. On Sonnet 5, medium
    # once produced ~2-min turns that blew the /cashflow report timeout (since raised to 600 s),
    # so report latency is re-checked after this change deploys.
    assert s.CHAT_THINKING_DEFAULT_LEVEL == "med"


def test_low_is_never_the_default():
    assert Settings().CHAT_THINKING_DEFAULT_LEVEL != "low"


def test_thinking_default_level_is_a_valid_level():
    from app.services.chat import thinking

    s = Settings()
    assert s.CHAT_THINKING_DEFAULT_LEVEL in thinking.LEVELS


def test_a_low_override_still_runs_at_med():
    # #355 packet review: the default alone did not enforce the floor. A deployment that set
    # CHAT_THINKING_DEFAULT_LEVEL=low still sent effort "low" to Sonnet 5.5 (reviewer's repro).
    from app.services.chat import thinking
    from app.services.chat.orchestrator import compute_thinking_level

    configured = Settings(_env_file=None, CHAT_THINKING_DEFAULT_LEVEL="low").CHAT_THINKING_DEFAULT_LEVEL
    level = compute_thinking_level(is_simple_lookup=False, enabled=True, default=configured)
    assert level == "med"
    assert thinking.anthropic_effort(level, "claude-sonnet-5-5") == "medium"


def test_no_provider_mapping_sends_low():
    # "low" stays parseable for older callers, but every mapping sends it as "med".
    from app.services.chat import thinking

    for model in ("claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-4-6"):
        assert thinking.anthropic_effort("low", model) == thinking.anthropic_effort("med", model)
    assert thinking.budget_for("low") == thinking.budget_for("med")
    assert thinking.reasoning_effort("low") == thinking.reasoning_effort("med")
