"""Unified LLM adapter interface for multi-provider AI support.

Provides a common interface (BaseLLMAdapter) so the orchestrator can work
with Anthropic, OpenAI, or Google Gemini via the same API surface.
"""

import abc
from dataclasses import dataclass, field


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    text_blocks: list[str] = field(default_factory=list)
    tool_use_blocks: list[ToolUseBlock] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)


class BaseLLMAdapter(abc.ABC):
    """Abstract base class for LLM provider adapters."""

    @abc.abstractmethod
    async def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Send a message to the LLM and return a normalized response."""

    async def stream_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        """Send a message to the LLM and yield streaming events, finishing with LLMResponse.

        Default implementation falls back to non-streaming create_message.
        Subclasses can override for true streaming support.
        """
        response = await self.create_message(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        # Emit all text as a single chunk then yield the full response
        for text in response.text_blocks:
            yield "text", text
        yield "response", response

    @abc.abstractmethod
    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        """Build a message containing tool results in the provider's format."""

    @abc.abstractmethod
    def build_assistant_message(self, response: LLMResponse) -> dict:
        """Build an assistant message from the response in the provider's format."""


# Provider-to-default-model mapping
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-5-20250929",
    "openai": "gpt-5.2",
    "gemini": "gemini-2.5-flash",
}

VALID_PROVIDERS = {"anthropic", "openai", "gemini"}

VALID_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-6",
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-5-20251101",
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
    ],
    "openai": [
        "gpt-5.2",
        "gpt-5.2-pro",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "o3",
        "o3-mini",
        "o3-pro",
        "o4-mini",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
    ],
}


def get_adapter(provider: str, api_key: str) -> BaseLLMAdapter:
    """Factory function to get the appropriate adapter for a provider."""
    if provider == "anthropic":
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(api_key=api_key)
    elif provider == "openai":
        from app.services.chat.adapters.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(api_key=api_key)
    elif provider == "gemini":
        from app.services.chat.adapters.gemini_adapter import GeminiAdapter

        return GeminiAdapter(api_key=api_key)
    else:
        raise ValueError(f"Unsupported AI provider: {provider}")
