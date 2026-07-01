# backend/tests/test_thinking_config.py
from app.core.config import Settings


def test_thinking_defaults():
    s = Settings()
    assert s.CHAT_THINKING_ENABLED is True
    # "low" is Anthropic's recommended effort for chat / latency-sensitive workloads;
    # "medium" on Sonnet 5 (~Sonnet 4.6 at high effort) produced ~2-min turns that blew
    # the report timeout. escalate_reasoning still lifts hard turns. (see /cashflow fix)
    assert s.CHAT_THINKING_DEFAULT_LEVEL == "low"


def test_thinking_default_level_is_a_valid_level():
    from app.services.chat import thinking

    s = Settings()
    assert s.CHAT_THINKING_DEFAULT_LEVEL in thinking.LEVELS
