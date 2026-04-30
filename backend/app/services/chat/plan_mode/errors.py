"""Plan Mode error types."""


class PlanModeUnsupportedError(Exception):
    """Raised when an LLM adapter doesn't support force_tool_choice.

    The orchestrator catches this and falls back to disabling Plan Mode for
    the turn (with logged warning telemetry) — never silently downgrades.
    """

    def __init__(self, provider: str, reason: str = "force_tool_choice not implemented") -> None:
        super().__init__(f"Plan Mode unsupported for provider={provider}: {reason}")
        self.provider = provider
        self.reason = reason
