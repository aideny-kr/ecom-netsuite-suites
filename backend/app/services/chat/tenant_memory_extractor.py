"""LLM concept extractor for the tenant memory graph.

Given a batch of tenant-specific source rows (learned rules, proven query
patterns, etc.), asks a fast model to distill them into reusable *concepts*
(definitions, entities, preferences) with relationships between them.

Mirrors ``memory_updater.py`` for the LLM call shape: a static prompt with a
``{{ROWS}}`` placeholder filled via string-replace (NOT f-string, so raw braces
in tenant text can't break assembly), ``re.search(r"\\{.*\\}", text, re.DOTALL)``
to find the JSON object, ``json.loads``, and a try/except that swallows any
failure into ``[]``.

The caller maps each extracted concept's ``plain_english_summary`` onto the
``summary`` column of ``tenant_memory_concepts`` at insert time.

Prompt hygiene (enforced by tests):
- **no-prompt-pollution**: the static prompt carries ONLY behavioral guidance —
  it never hardcodes tenant column/table script IDs. All tenant specifics arrive
  via the injected ``{{ROWS}}``.
- **no-LLM-numbers**: the model is explicitly told never to restate, compute,
  total, or invent numbers — concepts capture meaning, not figures.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.chat.llm_adapter import BaseLLMAdapter

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
You distill a tenant's accumulated knowledge into a small graph of reusable
CONCEPTS for an AI data assistant. Each source row below is something the tenant
has taught the assistant (a definition, an entity mapping, a preference, or a
proven query intent).

Produce CONCEPTS — durable, named ideas — and the EDGES between them. A concept
is a thing worth remembering (e.g. a metric definition, an entity, a naming
convention, an output preference). An edge links two concept names with a short
relationship label (e.g. "depends_on", "refines", "alias_of").

Rules:
- Extract only what the rows actually state. Do not invent concepts.
- Write each summary in plain English, as durable guidance — not as an answer to
  a one-off question.
- NEVER restate, compute, total, sum, or invent any numbers, amounts, counts, or
  figures. Concepts capture MEANING, not values. If a row mentions a figure,
  describe the rule behind it without reproducing the number.
- concept_type is one of: definition, entity, preference, convention, fact.
- confidence is a float 0.0-1.0 reflecting how clearly the rows support the
  concept.

Each source row below carries a "source_id". For every concept, list the
source_ids of the rows it was distilled from in a "source_ids" array, so the
evidence trail points back to the rows that actually support the concept. Use
only source_ids that appear in the rows; never invent one.

Return ONLY a JSON object of this exact shape (use [] when nothing applies):
{
  "concepts": [
    {
      "name": "short canonical name",
      "concept_type": "definition",
      "plain_english_summary": "1-2 sentence durable explanation, no numbers",
      "edges": [{"target": "other concept name", "relation": "depends_on"}],
      "source_ids": ["<source_id of a row this concept came from>"],
      "confidence": 0.0
    }
  ]
}

Source rows:
{{ROWS}}
"""


async def extract_concepts(
    rows: list[dict[str, Any]],
    adapter: BaseLLMAdapter,
    model: str,
) -> list[dict[str, Any]]:
    """Distill source rows into concept dicts via a single LLM call.

    Each returned dict carries ``name``, ``concept_type``,
    ``plain_english_summary`` (mapped to the ``summary`` column at insert time),
    ``edges``, and ``confidence``. Returns ``[]`` on no rows, no JSON, malformed
    JSON, a missing ``concepts`` key, or any adapter error — failures are
    swallowed, never raised.
    """
    if not rows:
        return []

    rows_text = json.dumps(rows, default=str)
    prompt = _EXTRACTION_PROMPT.replace("{{ROWS}}", rows_text)

    try:
        response = await adapter.create_message(
            model=model,
            max_tokens=1024,
            system="You distill tenant knowledge into reusable concepts. Return only JSON.",
            messages=[{"role": "user", "content": prompt}],
        )

        text = "\n".join(response.text_blocks) if response.text_blocks else ""
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            return []

        data = json.loads(json_match.group())
        concepts = data.get("concepts")
        if not isinstance(concepts, list):
            return []

        # Normalize source_ids to a list[str] on every concept so the backfill can
        # attribute each evidence link to the deriving concept (a missing/garbage
        # value becomes [] rather than a KeyError or a non-iterable).
        for concept in concepts:
            if not isinstance(concept, dict):
                continue
            raw_ids = concept.get("source_ids")
            concept["source_ids"] = [str(sid) for sid in raw_ids] if isinstance(raw_ids, list) else []
        return concepts

    except Exception:
        logger.warning("tenant_memory_extractor.extraction_failed", exc_info=True)
        return []


async def embed_concept(text: str) -> list[float] | None:
    """Embed concept text using OpenAI text-embedding-3-small (1536-dim).

    Returns ``None`` when no embedding key is configured or on any error.
    Mirrors ``query_pattern_service._embed_text``.
    """
    try:
        import openai

        from app.core.config import settings

        api_key = settings.OPENAI_EMBEDDING_API_KEY
        if not api_key:
            logger.warning("tenant_memory_extractor.no_embedding_key")
            return None

        client = openai.AsyncOpenAI(api_key=api_key)
        response = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
            dimensions=1536,
        )
        return response.data[0].embedding
    except Exception:
        logger.warning("tenant_memory_extractor.embedding_failed", exc_info=True)
        return None
