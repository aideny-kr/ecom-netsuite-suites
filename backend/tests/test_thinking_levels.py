from app.services.chat import thinking


def test_budget_for_known_levels():
    assert thinking.budget_for("none") == 0
    assert thinking.budget_for("low") == 2048
    assert thinking.budget_for("med") == 6144
    assert thinking.budget_for("high") == 12288
    assert thinking.budget_for("xhigh") == 24576


def test_budget_for_unknown_level_is_zero():
    assert thinking.budget_for("bogus") == 0
    assert thinking.budget_for(None) == 0


def test_next_level_escalates_one_step():
    assert thinking.next_level("none") == "med"
    assert thinking.next_level("low") == "high"
    assert thinking.next_level("med") == "high"
    assert thinking.next_level("high") == "xhigh"


def test_next_level_caps_at_xhigh():
    assert thinking.next_level("xhigh") == "xhigh"


def test_reasoning_effort_mapping():
    assert thinking.reasoning_effort("none") is None
    assert thinking.reasoning_effort("low") == "low"
    assert thinking.reasoning_effort("med") == "medium"
    assert thinking.reasoning_effort("high") == "high"
    # OpenAI/OpenRouter reasoning_effort enum is only low|medium|high — there is
    # no "xhigh", so our internal xhigh maps down to "high" (sending "xhigh" 400s).
    assert thinking.reasoning_effort("xhigh") == "high"


def test_is_forced_tool_choice():
    # Forced shapes — thinking must be suppressed (and the turn pinned off).
    assert thinking.is_forced_tool_choice({"type": "tool", "name": "clarify"}) is True
    assert thinking.is_forced_tool_choice({"type": "any"}) is True
    assert thinking.is_forced_tool_choice("any") is True
    assert thinking.is_forced_tool_choice("required") is True
    # Non-forced — thinking is compatible.
    assert thinking.is_forced_tool_choice({"type": "auto"}) is False
    assert thinking.is_forced_tool_choice({"type": "none"}) is False
    assert thinking.is_forced_tool_choice("auto") is False
    assert thinking.is_forced_tool_choice(None) is False
