# backend/tests/test_reasoning_prompt_reconcile.py
from app.services.chat.agents.unified_agent import build_reasoning_instruction


def test_reasoning_instruction_dropped_when_thinking_on():
    assert build_reasoning_instruction(thinking_enabled=True) == ""


def test_reasoning_instruction_present_when_thinking_off():
    text = build_reasoning_instruction(thinking_enabled=False)
    assert "<reasoning>" in text
