"""Per-message content summariser — generates factual summaries at write-time.

After each assistant response, a fast-model call produces a ~100-word summary
that captures key data points, conclusions, and record IDs. This summary is
stored on the ChatMessage and used in place of full content for older history
turns, eliminating the need for read-time compaction.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import update

from app.core.config import settings
from app.models.chat import ChatMessage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.chat.llm_adapter import BaseLLMAdapter

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """\
Summarise this chat exchange into a factual snapshot (max 100 words).
RETAIN:
- The user's question and intent
- Key data returned: totals, counts, dates, record IDs, field values, status codes
- Conclusions or answers given
- Any corrections, preferences, or constraints stated
DROP:
- Raw data tables and markdown formatting
- Pleasantries and filler
- Tool call JSON and code blocks
Output ONLY the summary, no preamble."""


async def generate_content_summary(
    user_message: str,
    assistant_message: str,
    adapter: "BaseLLMAdapter",
    model: str,
) -> str | None:
    """Generate a factual summary of a user-assistant exchange."""
    # Skip very short responses — they're already compact
    if len(assistant_message) < 200:
        return None

    messages = [
        {
            "role": "user",
            "content": (f"User: {user_message}\n\nAssistant: {assistant_message[:4000]}"),
        },
    ]

    try:
        response = await adapter.create_message(
            model=model,
            max_tokens=256,
            system=SUMMARY_PROMPT,
            messages=messages,
        )
        summary = "\n".join(response.text_blocks) if response.text_blocks else ""
        if summary.strip():
            return summary.strip()
    except Exception:
        logger.warning("content_summariser.failed", exc_info=True)

    return None


async def dispatch_content_summary(
    db: "AsyncSession",
    message_id: uuid.UUID,
    user_message: str,
    assistant_message: str,
) -> None:
    """Fire-and-forget: generate summary and persist to chat_messages."""
    from app.services.chat.llm_adapter import get_adapter

    adapter = get_adapter(
        settings.MULTI_AGENT_SPECIALIST_PROVIDER,
        settings.ANTHROPIC_API_KEY,
    )
    model = settings.MULTI_AGENT_SPECIALIST_MODEL

    try:
        summary = await generate_content_summary(
            user_message=user_message,
            assistant_message=assistant_message,
            adapter=adapter,
            model=model,
        )
        if summary:
            await db.execute(update(ChatMessage).where(ChatMessage.id == message_id).values(content_summary=summary))
            await db.commit()
            logger.info(
                "content_summariser.saved",
                extra={"message_id": str(message_id), "summary_len": len(summary)},
            )
    except Exception as e:
        logger.error(f"content_summariser.dispatch_failed: {e}")
