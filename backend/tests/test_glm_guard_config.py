# backend/tests/test_glm_guard_config.py
from app.core.config import Settings


def test_glm_tier_defaults_are_safe():
    s = Settings()
    assert s.CHAT_THINKING_MODEL == ""  # empty → escalate on tenant's own model
    assert s.CHAT_THINKING_PROVIDER == ""
    assert s.ALLOW_CHINA_ORIGIN_ON_CUSTOMER_DATA is False  # blocked by default
