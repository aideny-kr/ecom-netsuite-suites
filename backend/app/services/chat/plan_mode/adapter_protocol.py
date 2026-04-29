"""Protocol for LLM adapters that support tool_choice forcing."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ForceToolChoiceCapable(Protocol):
    """Adapter must implement this to support Plan Mode hard gate.

    Returns the API parameter dict for the underlying SDK to force a single
    tool call. Raises PlanModeUnsupportedError if the model/provider can't
    enforce tool choice (e.g., older Claude models without tool_choice param,
    Gemini models below 1.5 Pro, etc.).

    `model` is optional because Anthropic/OpenAI shapes are model-agnostic;
    Gemini needs it to gate function_calling_config to 1.5+ models.
    """

    def force_tool_choice(self, tool_name: str, model: str | None = None) -> dict: ...
