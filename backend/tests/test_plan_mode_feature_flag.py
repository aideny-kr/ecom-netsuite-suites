"""Verify plan_mode_enabled flag is recognized and defaults off."""

from app.services.feature_flag_service import get_default_value, is_known_flag


def test_plan_mode_enabled_is_known():
    """plan_mode_enabled must be in the known flag registry."""
    assert is_known_flag("plan_mode_enabled") is True


def test_plan_mode_enabled_defaults_off():
    """plan_mode_enabled defaults to False (off)."""
    assert get_default_value("plan_mode_enabled") is False


def test_unknown_flag_not_recognized():
    """Sanity: an unknown flag key returns False from both helpers."""
    assert is_known_flag("nope_does_not_exist") is False
    assert get_default_value("nope_does_not_exist") is False
