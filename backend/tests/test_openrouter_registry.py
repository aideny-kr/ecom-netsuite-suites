from app.services.chat.adapters.openrouter_adapter import OpenRouterAdapter
from app.services.chat.llm_adapter import (
    DEFAULT_MODELS,
    VALID_MODELS,
    VALID_PROVIDERS,
    get_adapter,
)


def test_openrouter_is_a_valid_provider():
    assert "openrouter" in VALID_PROVIDERS
    assert "openai/gpt-4o-mini" in VALID_MODELS["openrouter"]
    assert DEFAULT_MODELS["openrouter"]


def test_openrouter_does_not_expose_china_origin_models():
    # China-origin models (GLM/DeepSeek/Qwen) must NOT be selectable until the
    # residency guard is wired — otherwise a BYOK tenant could route customer
    # data to them unguarded.
    for model in VALID_MODELS["openrouter"]:
        assert not model.startswith("z-ai/")
        assert "glm" not in model.lower()


def test_factory_returns_openrouter_adapter():
    adapter = get_adapter("openrouter", "sk-or-test")
    assert isinstance(adapter, OpenRouterAdapter)


def test_openrouter_api_key_setting_exists():
    from app.core.config import Settings

    assert hasattr(Settings(), "OPENROUTER_API_KEY")
