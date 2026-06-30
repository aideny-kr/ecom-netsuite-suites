# backend/tests/test_openrouter_adapter.py
from app.services.chat.adapters.openrouter_adapter import OpenRouterAdapter


def test_base_url_points_at_openrouter():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    assert "openrouter.ai/api/v1" in str(adapter._client.base_url)


def test_timeout_is_non_default():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    assert adapter._client.timeout.read <= 120
    assert adapter._client.timeout.connect <= 10


def test_provider_pins_are_zdr_and_no_logging():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    pins = adapter._provider_pins()
    assert pins["data_collection"] == "deny"
    assert pins["zdr"] is True
    # No provider allowlist: an open-weight-host allowlist would exclude OpenAI and
    # break routing for the only exposed model (gpt-4o-mini). zdr already gates routing.
    assert "only" not in pins


def test_reasoning_effort_threaded_into_extra_body():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    body = adapter._extra_body(thinking_level="high")
    assert body["reasoning_effort"] == "high"
    assert body["provider"]["zdr"] is True


def test_reasoning_omitted_for_none():
    adapter = OpenRouterAdapter(api_key="sk-or-test")
    body = adapter._extra_body(thinking_level="none")
    assert "reasoning_effort" not in body
