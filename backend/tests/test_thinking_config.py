# backend/tests/test_thinking_config.py
from app.core.config import Settings


def test_thinking_defaults():
    s = Settings()
    assert s.CHAT_THINKING_ENABLED is True
    assert s.CHAT_THINKING_DEFAULT_LEVEL == "med"


def test_thinking_default_level_is_a_valid_level():
    from app.services.chat import thinking

    s = Settings()
    assert s.CHAT_THINKING_DEFAULT_LEVEL in thinking.LEVELS
