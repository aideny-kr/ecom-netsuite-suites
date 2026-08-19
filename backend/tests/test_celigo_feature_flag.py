"""Verify the celigo flag is registered in the flag registry and defaults off.

useFeature("celigo") (frontend/src/app/(dashboard)/settings/page.tsx) reads a key
that was absent from DEFAULT_FLAGS -- enablable via bulk-set (which doesn't
validate keys), but not seeded for new tenants and invisible to an operator
reading the registry to discover flags. Mirrors test_plan_mode_feature_flag.py.
"""

from app.services.feature_flag_service import get_default_value, is_known_flag


def test_celigo_is_known():
    """celigo must be in the known flag registry."""
    assert is_known_flag("celigo") is True


def test_celigo_defaults_off():
    """celigo defaults to False (off) -- Plan A ships behind a default-off flag."""
    assert get_default_value("celigo") is False
