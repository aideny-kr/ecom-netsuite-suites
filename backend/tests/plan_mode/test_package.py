"""Verify plan_mode package skeleton imports."""


def test_plan_mode_package_imports():
    from app.services.chat import plan_mode
    assert plan_mode is not None


def test_ambiguity_signal_module_imports():
    from app.services.chat.plan_mode import ambiguity_signal
    assert ambiguity_signal is not None


def test_clarify_intercept_module_imports():
    from app.services.chat.plan_mode import clarify_intercept
    assert clarify_intercept is not None


def test_clarify_tool_module_imports():
    from app.services.chat.plan_mode import clarify_tool
    assert clarify_tool is not None
