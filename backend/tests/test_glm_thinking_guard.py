from app.services.chat.thinking import resolve_escalation_target


def test_china_origin_blocked_on_customer_data_without_guard():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6",
        tenant_provider="anthropic",
        configured_model="z-ai/glm-5.2",
        configured_provider="openrouter",
        flag_enabled=True,
        allow_china_origin=False,
        is_customer_data=True,
    )
    # Blocked → fall back to the tenant's own model/provider
    assert target == ("claude-sonnet-4-6", "anthropic")


def test_china_origin_allowed_when_guard_and_flag_set():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6",
        tenant_provider="anthropic",
        configured_model="z-ai/glm-5.2",
        configured_provider="openrouter",
        flag_enabled=True,
        allow_china_origin=True,
        is_customer_data=True,
    )
    assert target == ("z-ai/glm-5.2", "openrouter")


def test_flag_off_uses_native_fallback():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6",
        tenant_provider="anthropic",
        configured_model="z-ai/glm-5.2",
        configured_provider="openrouter",
        flag_enabled=False,
        allow_china_origin=True,
        is_customer_data=True,
    )
    assert target == ("claude-sonnet-4-6", "anthropic")


def test_non_china_configured_model_allowed_without_china_guard():
    target = resolve_escalation_target(
        tenant_model="claude-sonnet-4-6",
        tenant_provider="anthropic",
        configured_model="openai/gpt-4o-mini",
        configured_provider="openrouter",
        flag_enabled=True,
        allow_china_origin=False,
        is_customer_data=True,
    )
    assert target == ("openai/gpt-4o-mini", "openrouter")
