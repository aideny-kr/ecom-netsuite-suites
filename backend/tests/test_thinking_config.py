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
