"""Verify every LLM adapter overrides the SDK default timeouts.

Without explicit timeouts the Anthropic/OpenAI SDKs default to a 600s read
timeout and a Gemini client has no app-level timeout at all. A single stalled
socket can then burn the entire 300s chat budget and strand the user on a blank
screen. These tests pin the adapters to interactive-chat-friendly deadlines so
a future `__init__` refactor cannot silently regress to the defaults.
"""

from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
from app.services.chat.adapters.gemini_adapter import GeminiAdapter
from app.services.chat.adapters.openai_adapter import OpenAIAdapter


class TestAdapterTimeouts:
    """Adapters must cap reads well below the 300s outer chat budget."""

    def test_anthropic_adapter_has_non_default_timeout(self):
        adapter = AnthropicAdapter(api_key="sk-test")
        timeout = adapter._client.timeout

        assert timeout.read <= 120, (
            f"Anthropic read timeout {timeout.read}s would allow a single "
            "stalled request to exceed our 300s chat budget. Expected ≤120s."
        )
        assert timeout.connect <= 10, f"Anthropic connect timeout {timeout.connect}s is too loose. Expected ≤10s."

    def test_openai_adapter_has_non_default_timeout(self):
        adapter = OpenAIAdapter(api_key="sk-test")
        timeout = adapter._client.timeout

        assert timeout.read <= 120, (
            f"OpenAI read timeout {timeout.read}s would allow a single "
            "stalled request to exceed our 300s chat budget. Expected ≤120s."
        )
        assert timeout.connect <= 10, f"OpenAI connect timeout {timeout.connect}s is too loose. Expected ≤10s."

    def test_gemini_adapter_has_non_default_timeout(self):
        adapter = GeminiAdapter(api_key="test-key")
        http_options = adapter._client._api_client._http_options

        timeout_ms = getattr(http_options, "timeout", None)
        assert timeout_ms is not None, "GeminiAdapter must configure http_options.timeout to bound stalled reads."
        assert timeout_ms <= 120_000, f"Gemini timeout {timeout_ms}ms exceeds our 300s chat budget. Expected ≤120000ms."
