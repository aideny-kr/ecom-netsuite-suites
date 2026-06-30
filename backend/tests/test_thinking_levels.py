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


def test_anthropic_effort_mapping():
    assert thinking.anthropic_effort("none") is None
    assert thinking.anthropic_effort("low") == "low"
    assert thinking.anthropic_effort("med") == "medium"
    assert thinking.anthropic_effort("high") == "high"
    # xhigh is MODEL-AWARE: valid on Sonnet 5 / Opus 4.7+ / Fable; on adaptive models
    # that lack xhigh (Sonnet 4.6 / Opus 4.6) it maps to "max" (xhigh would 400 there).
    assert thinking.anthropic_effort("xhigh", "claude-sonnet-5") == "xhigh"
    assert thinking.anthropic_effort("xhigh", "claude-opus-4-8") == "xhigh"
    assert thinking.anthropic_effort("xhigh", "claude-sonnet-4-6") == "max"
    assert thinking.anthropic_effort("xhigh", "claude-opus-4-6") == "max"


def test_thinking_mode_classifies_models():
    # Adaptive thinking + effort: Sonnet 5 / 4.6 / Opus 4.6+ / Fable.
    for m in [
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-opus-4-8",
        "claude-fable-5",
    ]:
        assert thinking.thinking_mode(m) == "adaptive", m
    # Legacy extended thinking (budget_tokens): 4.5 / 4.0 / 4.1 AND Haiku (Haiku
    # supports extended thinking but not the effort param).
    for m in [
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5-20251101",
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-opus-4-1-20250805",
        "claude-haiku-4-5-20251001",
    ]:
        assert thinking.thinking_mode(m) == "legacy", m


def test_sonnet_5_is_the_anthropic_default_and_adaptive():
    from app.services.chat.llm_adapter import DEFAULT_MODELS, VALID_MODELS

    assert DEFAULT_MODELS["anthropic"] == "claude-sonnet-5"
    assert "claude-sonnet-5" in VALID_MODELS["anthropic"]
    assert thinking.thinking_mode("claude-sonnet-5") == "adaptive"
