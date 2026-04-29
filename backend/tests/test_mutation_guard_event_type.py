"""Regression tests for mutation_guard event_type extension.

CRITICAL: extending generate_confirmation_token / verify_confirmation_token
with event_type must NOT break existing write-confirm tokens. The default
event_type='write_confirm' must hash IDENTICALLY to the pre-existing
implementation so PR #39 callers keep working with zero changes.
"""

from app.services.chat.mutation_guard import (
    generate_confirmation_token,
    verify_confirmation_token,
)


def test_default_event_type_preserves_write_confirm_behavior():
    """Tokens generated WITHOUT event_type validate WITHOUT event_type."""
    token = generate_confirmation_token("sess-1", '{"k":"v"}')
    assert verify_confirmation_token(token, "sess-1", '{"k":"v"}') is True


def test_explicit_write_confirm_event_type_matches_default():
    """Explicit event_type='write_confirm' = default unspecified — same token."""
    t_default = generate_confirmation_token("sess-1", '{"k":"v"}')
    t_explicit = generate_confirmation_token("sess-1", '{"k":"v"}', event_type="write_confirm")
    assert t_default == t_explicit


def test_plan_mode_choice_event_type_works():
    token = generate_confirmation_token("sess-1", '{"opts":[]}', event_type="plan_mode_choice")
    assert verify_confirmation_token(token, "sess-1", '{"opts":[]}', event_type="plan_mode_choice") is True


def test_event_type_mismatch_rejects():
    """Token bound to write_confirm must NOT validate as plan_mode_choice."""
    token = generate_confirmation_token("sess-1", '{"k":"v"}', event_type="write_confirm")
    assert verify_confirmation_token(token, "sess-1", '{"k":"v"}', event_type="plan_mode_choice") is False


def test_default_token_rejected_with_explicit_plan_mode_event_type():
    """A pre-existing PR #39 token (no event_type) won't validate as plan_mode."""
    token = generate_confirmation_token("sess-1", '{"k":"v"}')  # default = write_confirm
    assert verify_confirmation_token(token, "sess-1", '{"k":"v"}', event_type="plan_mode_choice") is False


def test_session_mismatch_still_rejects():
    token = generate_confirmation_token("sess-1", '{"k":"v"}', event_type="plan_mode_choice")
    assert verify_confirmation_token(token, "sess-2", '{"k":"v"}', event_type="plan_mode_choice") is False


def test_payload_mismatch_still_rejects():
    token = generate_confirmation_token("sess-1", '{"k":"v"}', event_type="plan_mode_choice")
    assert verify_confirmation_token(token, "sess-1", '{"k":"DIFFERENT"}', event_type="plan_mode_choice") is False


def test_two_event_types_produce_distinct_tokens():
    """Same session + payload, different event_types → different tokens."""
    t1 = generate_confirmation_token("sess-1", '{"k":"v"}', event_type="write_confirm")
    t2 = generate_confirmation_token("sess-1", '{"k":"v"}', event_type="plan_mode_choice")
    assert t1 != t2
