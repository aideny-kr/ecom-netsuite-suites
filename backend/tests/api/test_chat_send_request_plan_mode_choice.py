"""Verify SendMessageRequest accepts plan_mode_choice field."""

from app.api.v1.chat import SendMessageRequest


def test_plan_mode_choice_optional():
    """Field is optional; existing requests without it still validate."""
    req = SendMessageRequest(content="hi")
    assert req.plan_mode_choice is None


def test_plan_mode_choice_accepts_dict():
    req = SendMessageRequest(
        content="resume",
        plan_mode_choice={
            "action": "approve",
            "confirmation_id": "msg-123",
            "option_id": "A",
        },
    )
    assert req.plan_mode_choice["action"] == "approve"
    assert req.plan_mode_choice["option_id"] == "A"
    assert req.plan_mode_choice["confirmation_id"] == "msg-123"


def test_write_confirm_field_still_works():
    """Regression: existing write_confirm field is unaffected."""
    req = SendMessageRequest(
        content="approve",
        write_confirm={"action": "approve", "confirmation_id": "msg-456"},
    )
    assert req.write_confirm["action"] == "approve"


def test_both_fields_can_coexist():
    """Schema-wise the two fields can both be set; validation/precedence is the orchestrator's job."""
    req = SendMessageRequest(
        content="x",
        write_confirm={"action": "approve", "confirmation_id": "msg-1"},
        plan_mode_choice={"action": "approve", "confirmation_id": "msg-2", "option_id": "B"},
    )
    assert req.write_confirm is not None
    assert req.plan_mode_choice is not None
