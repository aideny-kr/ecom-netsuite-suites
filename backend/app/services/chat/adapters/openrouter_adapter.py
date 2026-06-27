"""OpenRouter adapter — OpenAI-API-compatible gateway.

Subclasses OpenAIAdapter (OpenRouter speaks the OpenAI Chat Completions API) and
overrides only the base_url, key, attribution headers, provider-routing pins
(US hosts + Zero-Data-Retention), and reasoning_effort threading.

RESIDENCY: provider pins restrict routing to US-hosted endpoints with ZDR. China
-origin models (GLM/DeepSeek/Qwen) are intentionally NOT exposed in
VALID_MODELS["openrouter"] yet — only US models (e.g. openai/gpt-4o-mini). Do not
re-add China-origin models here until a residency guard gates them on
customer-data paths (and they clear the Claude+MCP benchmark).
"""

import httpx
import openai

from app.services.chat import thinking as _thinking
from app.services.chat.adapters.openai_adapter import OpenAIAdapter

_CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=60.0)
_CLIENT_MAX_RETRIES = 2
_BASE_URL = "https://openrouter.ai/api/v1"

# US-hosted providers we permit OpenRouter to route to. Tighten/loosen here.
_US_PROVIDER_ALLOWLIST = ["DeepInfra", "Together", "Fireworks", "Baseten"]


class OpenRouterAdapter(OpenAIAdapter):
    def __init__(self, api_key: str):
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=_BASE_URL,
            timeout=_CLIENT_TIMEOUT,
            max_retries=_CLIENT_MAX_RETRIES,
            default_headers={
                "HTTP-Referer": "https://suitestudio.ai",
                "X-Title": "Suite Studio",
            },
        )

    def _provider_pins(self) -> dict:
        """OpenRouter `provider` routing constraints: US hosts + ZDR + no logging."""
        return {"only": list(_US_PROVIDER_ALLOWLIST), "data_collection": "deny", "zdr": True}

    def _extra_body(self, *, thinking_level: str | None) -> dict:
        body: dict = {"provider": self._provider_pins()}
        effort = _thinking.reasoning_effort(thinking_level)
        if effort is not None:
            body["reasoning_effort"] = effort
        return body

    async def create_message(self, *, thinking_level: str | None = None, **kwargs):
        # OpenAI SDK forwards unknown params via extra_body; inject provider pins
        # + reasoning_effort there so the parent's request building is untouched.
        kwargs["extra_body"] = self._extra_body(thinking_level=thinking_level)
        return await super().create_message(**kwargs)

    async def stream_message(self, *, thinking_level: str | None = None, **kwargs):
        kwargs["extra_body"] = self._extra_body(thinking_level=thinking_level)
        async for ev in super().stream_message(**kwargs):
            yield ev
