# backend/tests/test_orchestrator_thinking_level.py
from app.services.chat.orchestrator import compute_thinking_level


def test_simple_lookup_gets_none():
    assert compute_thinking_level(is_simple_lookup=True, enabled=True, default="med") == "none"


def test_normal_turn_gets_default():
    assert compute_thinking_level(is_simple_lookup=False, enabled=True, default="med") == "med"


def test_kill_switch_forces_none():
    assert compute_thinking_level(is_simple_lookup=False, enabled=False, default="high") == "none"
