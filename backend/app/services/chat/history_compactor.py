"""History compaction — summarise old conversation turns to reduce token usage.

When a conversation exceeds COMPACTION_THRESHOLD turns, the oldest turns are
replaced with a dense LLM-generated summary while the most recent exchanges
are preserved verbatim. This cuts 2,000-6,000 tokens per turn on long
conversations at the cost of one fast-model call (~500 tokens).

Fails gracefully — if compaction errors, the original history is returned unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat.llm_adapter import BaseLLMAdapter

logger = logging.getLogger(__name__)

# Minimum number of messages before compaction triggers.
# Each user-assistant exchange = 2 messages, so 12 messages = 6 turns.
COMPACTION_THRESHOLD = 12

# Number of recent messages to preserve verbatim (last 2 user-assistant exchanges).
KEEP_RECENT = 4

COMPACTION_PROMPT = """\
Summarise this conversation into a dense snapshot for an AI assistant.
RETAIN:
1. The user's current goal and any constraints they stated
2. Key data points mentioned (numbers, dates, record IDs, field names)
3. Strategies or queries that FAILED (to avoid repeating)
4. Any corrections or preferences the user stated
DROP: Pleasantries, raw data dumps, repeated questions, tool call JSON, markdown tables.
Output a concise summary (max 300 words).
"""


async def compact_history(
    history: list[dict],
    adapter: BaseLLMAdapter,
    model: str,
) -> list[dict]:
    """Compact old history turns into a summary + keep recent turns.

    Returns a new history list: [compacted_summary, ack, ...recent_turns].
    If history is short enough, returns it unchanged.
    """
    if len(history) <= COMPACTION_THRESHOLD:
        return history

    old_turns = history[:-KEEP_RECENT]
    recent_turns = history[-KEEP_RECENT:]

    # Ask the LLM to summarise old turns
    summary_messages = list(old_turns) + [
        {"role": "user", "content": COMPACTION_PROMPT},
    ]

    try:
        response = await adapter.create_message(
            model=model,
            max_tokens=512,
            system="You are a conversation summariser. Output only the summary.",
            messages=summary_messages,
        )

        summary_text = "\n".join(response.text_blocks) if response.text_blocks else ""
        if not summary_text.strip():
            logger.warning("history_compactor.empty_summary")
            return history

        logger.info(
            "history_compactor.compacted",
            extra={
                "old_turns": len(old_turns),
                "summary_len": len(summary_text),
                "kept_recent": len(recent_turns),
            },
        )

        compacted: list[dict] = [
            {
                "role": "user",
                "content": f"<compacted_history>\n{summary_text}\n</compacted_history>",
            },
            {
                "role": "assistant",
                "content": "Understood. I have the conversation context.",
            },
        ]
        compacted.extend(recent_turns)
        return compacted

    except Exception:
        logger.warning("history_compactor.failed", exc_info=True)
        return history
